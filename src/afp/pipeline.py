"""
Pipeline orchestrator: wires repo eligibility, commit collection, PR
collection, heuristics and feature extraction into one run per repo.
batch.py runs this over a whole corpus.

Two modes:
  - git-only (no GITHUB_TOKEN): clone + commit collection + file markers
    + LOC + features. Eligibility checks that need the API (stars,
    contributors, branch protection) and PR-level data are skipped and
    reported as such.
  - full (GITHUB_TOKEN set): everything, including PR-level data, PR ->
    merge-commit label propagation and a proper eligibility verdict.

Output of one run is a dict of lists of plain dicts (JSON-serializable),
written to JSONL files by export_jsonl().
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .adoption import summarize_repo
from .collect import (clone_repo, collect_commits, compute_edit_velocity, count_commits,
                      count_loc, find_file_markers, head_tree, is_shallow, remove_tree)
from .features import commits_to_feature_vectors
from .heuristics import apply_pr_label
from .schema import EligibilityResult, RepoAdoption, RepoMetadata


class PipelineResult:
    def __init__(self, full_name: str = ""):
        self.full_name = full_name
        self.repo_metadata: Optional[RepoMetadata] = None
        self.eligibility: Optional[EligibilityResult] = None
        self.commits: List[Any] = []
        self.pull_requests: List[Any] = []
        self.feature_vectors: List[Any] = []
        self.file_markers: List[Any] = []
        self.adoption: Optional[RepoAdoption] = None
        self.mode: str = "git-only"
        self.window: Dict[str, Optional[str]] = {"since": None, "until": None}
        self.notes: List[str] = []
        self.status: str = "ok"
        self.error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "full_name": self.full_name,
            "status": self.status,
            "error": self.error,
            "mode": self.mode,
            "window": self.window,
            "notes": self.notes,
            "repo_metadata": self.repo_metadata.to_dict() if self.repo_metadata else None,
            "eligibility": self.eligibility.to_dict() if self.eligibility else None,
            "adoption": self.adoption.to_dict() if self.adoption else None,
            "commit_count": len(self.commits),
            "pr_count": len(self.pull_requests),
            "feature_vector_count": len(self.feature_vectors),
            "file_marker_count": len(self.file_markers),
        }


def _propagate_pr_labels(commits, prs) -> int:
    """Label the commit GitHub created when merging an agent PR, and mark
    squash commits as such so edit velocity excludes them. Returns the
    number of commits that gained an agent label this way."""
    by_sha = {c.sha: c for c in commits}
    gained = 0
    for pr in prs:
        commit = by_sha.get(pr.merge_commit_sha or "")
        if commit is None:
            continue
        if pr.merged_via == "squash" and not commit.is_merge:
            commit.merge_type = "squash"
        was_agent = commit.label.label == "agent_labeled"
        apply_pr_label(commit.label, pr.label, pr.number)
        if not was_agent and commit.label.label == "agent_labeled":
            gained += 1
    return gained


def run_pipeline(owner: str, repo: str, work_dir: Optional[str] = None,
                 branch: Optional[str] = None, shallow_since: Optional[str] = None,
                 collect_prs: bool = True, keep_clone: bool = False,
                 until: Optional[str] = None, client=None,
                 max_prs: Optional[int] = None, as_of: Optional[str] = None) -> PipelineResult:
    """Run every stage for one repo. `shallow_since`/`until` (YYYY-MM-DD)
    bound the commit and PR window; `as_of` (YYYY-MM-DD, default today) is
    the date "pushed in the last 12 months" is measured from; `client` lets
    a batch share one rate-limited GitHubClient across workers (default:
    built from GITHUB_TOKEN)."""
    full_name = f"{owner}/{repo}"
    result = PipelineResult(full_name)
    result.window = {"since": shallow_since, "until": until}

    token = os.environ.get("GITHUB_TOKEN")
    if client is None and token:
        from .github_client import GitHubClient
        client = GitHubClient(token=token)

    tmp_ctx = None
    if work_dir is None:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="afp_")
        work_dir = tmp_ctx.name
    clone_path = str(Path(work_dir) / f"{owner}__{repo}")

    try:
        # --- 1. clone + commit collection (always runs, no token needed) ---
        clone_url = f"https://github.com/{owner}/{repo}.git"
        clone_repo(clone_url, clone_path, shallow_since=shallow_since)
        commits = collect_commits(clone_path, full_name, branch=branch, since=shallow_since, until=until)
        result.commits = commits

        tree = head_tree(clone_path)
        total_loc = count_loc(clone_path, tree)
        result.file_markers = find_file_markers(clone_path, [e.path for e in tree])
        git_commit_count = None if is_shallow(clone_path) else count_commits(clone_path)

        meta = RepoMetadata(owner=owner, name=repo, full_name=full_name, total_loc=total_loc,
                            commit_count=git_commit_count,
                            commit_count_source="git" if git_commit_count is not None else None)
        result.repo_metadata = meta

        # --- 2. metadata + eligibility + PRs (need a working token) ---
        if client is not None:
            try:
                from .eligibility import build_repo_metadata, check_eligibility

                meta = build_repo_metadata(client, owner, repo, total_loc=total_loc)
                if meta.commit_count is None and git_commit_count is not None:
                    meta.commit_count, meta.commit_count_source = git_commit_count, "git"
                result.repo_metadata = meta
                now = datetime.fromisoformat(f"{as_of}T23:59:59+00:00") if as_of else None
                result.eligibility = check_eligibility(meta, now=now)
                result.mode = "full"

                if collect_prs:
                    from .pr_collect import collect_pull_requests
                    result.pull_requests = collect_pull_requests(
                        client, owner, repo, since=shallow_since, until=until, max_prs=max_prs)
                    gained = _propagate_pr_labels(commits, result.pull_requests)
                    if gained:
                        result.notes.append(f"{gained} merge/squash commit(s) labelled from their agent PR")
            except LookupError:
                raise
            except Exception as exc:
                # A GITHUB_TOKEN being *set* doesn't guarantee the API call
                # will actually succeed (wrong scope, revoked, or a
                # placeholder value). Degrade to git-only rather than
                # crashing the whole run.
                result.notes.append(
                    f"GitHub API stage failed ({exc.__class__.__name__}: {exc}); kept git-only "
                    "results. Commit collection via git log still ran fully. Check that "
                    "GITHUB_TOKEN is a valid personal access token."
                )
        else:
            result.notes.append(
                "GITHUB_TOKEN not set: skipped repo metadata, eligibility checks "
                "that need the API (stars/contributors/branch protection), "
                "and PR-level data collection. Commit collection via git log ran fully."
            )

        # --- 3. features (always runs, derived only from commit data) ---
        # after PR propagation: squash commits must be known before ranking velocity
        compute_edit_velocity(commits)
        result.feature_vectors = commits_to_feature_vectors(commits)
        result.adoption = summarize_repo(full_name, commits, result.pull_requests, result.file_markers)

    finally:
        if not keep_clone:
            remove_tree(clone_path)
        if tmp_ctx is not None and not keep_clone:
            tmp_ctx.cleanup()

    return result


def _records_to_dicts(records: List[Any]) -> List[Dict[str, Any]]:
    out = []
    for r in records:
        out.append(r.to_dict() if hasattr(r, "to_dict") else asdict(r))
    return out


def export_jsonl(result: PipelineResult, out_dir: str) -> Dict[str, str]:
    """Write commits.jsonl, prs.jsonl, features.jsonl, file_markers.jsonl
    and summary.json (UTF-8) to out_dir. Returns the paths written.
    summary.json is written last: its presence marks the repo as done."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {}

    def _write_jsonl(name: str, records: List[Any]):
        path = out / name
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for rec_dict in _records_to_dicts(records):
                f.write(json.dumps(rec_dict, default=str, ensure_ascii=False) + "\n")
        paths[name] = str(path)

    _write_jsonl("commits.jsonl", result.commits)
    _write_jsonl("prs.jsonl", result.pull_requests)
    _write_jsonl("features.jsonl", result.feature_vectors)
    _write_jsonl("file_markers.jsonl", result.file_markers)

    summary_path = out / "summary.json"
    tmp_path = out / "summary.json.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2, default=str, ensure_ascii=False)
    os.replace(tmp_path, summary_path)
    paths["summary.json"] = str(summary_path)

    return paths
