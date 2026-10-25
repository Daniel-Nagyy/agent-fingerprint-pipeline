"""
Pipeline orchestrator: wires repo eligibility, commit collection, PR and
issue collection, repo scanning, heuristics and feature extraction into
one run per repo. batch.py runs this over a whole corpus.

Modes:
  - clone (default): commits from a shallow `git clone --no-checkout`
    (full diff stats and changed paths, so every feature, LOC and file
    first-added dates). The API stage (metadata, PRs, issues) runs in
    parallel with the clone. Without GITHUB_TOKEN it degrades to
    git-only: commits, file markers, repo signals, LOC and features.
  - api: no clone at all. Commits (with author logins and line counts),
    the file tree and the few files repo_scan needs all come from the
    GitHub API. Much faster for adoption detection on many repos; the
    file-type-mix features and LOC are not available. Needs a token.

Output of one run is a dict of lists of plain dicts (JSON-serializable),
written to JSONL files by export_jsonl().
"""

from __future__ import annotations

import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import repo_scan
from .adoption import summarize_repo
from .collect import (clone_repo, collect_commits, compute_edit_velocity, count_commits, count_loc,
                      find_file_markers, first_added, head_tree, is_shallow, read_blobs, remove_tree)
from .features import commits_to_feature_vectors
from .heuristics import apply_pr_label
from .schema import EligibilityResult, FileMarker, RepoAdoption, RepoMetadata
from .signatures import file_marker_brand

MODES = ("clone", "api")


class PipelineResult:
    def __init__(self, full_name: str = ""):
        self.full_name = full_name
        self.repo_metadata: Optional[RepoMetadata] = None
        self.eligibility: Optional[EligibilityResult] = None
        self.commits: List[Any] = []
        self.pull_requests: List[Any] = []
        self.issues: List[Any] = []
        self.feature_vectors: List[Any] = []
        self.file_markers: List[Any] = []
        self.signals: List[Any] = []
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
            "issue_count": len(self.issues),
            "feature_vector_count": len(self.feature_vectors),
            "file_marker_count": len(self.file_markers),
            "signal_count": len(self.signals),
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


def _markers_from_paths(paths: List[str]) -> List[FileMarker]:
    markers, seen = [], set()
    for rel in paths:
        hit = file_marker_brand(rel)
        if not hit:
            continue
        marker, brand = hit
        key = (marker, rel.rsplit("/", 1)[0] if marker.endswith("/") else rel)
        if key not in seen:
            seen.add(key)
            markers.append(FileMarker(path=rel, marker=marker, brand=brand))
    return markers[:50]


def run_pipeline(owner: str, repo: str, work_dir: Optional[str] = None,
                 branch: Optional[str] = None, shallow_since: Optional[str] = None,
                 collect_prs: bool = True, keep_clone: bool = False,
                 until: Optional[str] = None, client=None,
                 max_prs: Optional[int] = None, as_of: Optional[str] = None,
                 mode: str = "clone", collect_issues: bool = True,
                 max_issues: int = 200, max_commits: int = 2000) -> PipelineResult:
    """Run every stage for one repo. `shallow_since`/`until` (YYYY-MM-DD)
    bound the commit, PR and issue window; `as_of` (YYYY-MM-DD, default
    today) is the date "pushed in the last 12 months" is measured from;
    `client` lets a batch share one rate-limited GitHubClient (and its
    token pool) across workers (default: built from GITHUB_TOKEN(S))."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    full_name = f"{owner}/{repo}"
    result = PipelineResult(full_name)
    result.window = {"since": shallow_since, "until": until}

    if client is None:
        from .github_client import GitHubClient, tokens_from_env
        if tokens_from_env():
            client = GitHubClient()
    if mode == "api" and client is None:
        raise ValueError("--mode api needs GITHUB_TOKEN or GITHUB_TOKENS")

    # --- API stage, started first so it overlaps the clone ---------------
    pool = ThreadPoolExecutor(max_workers=3) if client is not None else None
    futures: Dict[str, Any] = {}
    if pool is not None:
        from .eligibility import build_repo_metadata
        futures["meta"] = pool.submit(build_repo_metadata, client, owner, repo)
        if collect_prs:
            from .pr_collect import collect_pull_requests
            futures["prs"] = pool.submit(collect_pull_requests, client, owner, repo,
                                         since=shallow_since, until=until, max_prs=max_prs)
        if collect_issues:
            from .issue_collect import collect_issues as _collect_issues
            futures["issues"] = pool.submit(_collect_issues, client, owner, repo,
                                            since=shallow_since, until=until, max_issues=max_issues)

    tmp_ctx = None
    if work_dir is None:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="afp_")
        work_dir = tmp_ctx.name
    clone_path = str(Path(work_dir) / f"{owner}__{repo}")
    total_loc = git_commit_count = None

    try:
        if mode == "clone":
            # --- 1. clone + commits + files (no token needed) -------------
            clone_repo(f"https://github.com/{owner}/{repo}.git", clone_path, shallow_since=shallow_since)
            commits = collect_commits(clone_path, full_name, branch=branch, since=shallow_since, until=until)
            tree = head_tree(clone_path)
            paths = [e.path for e in tree]
            total_loc = count_loc(clone_path, tree)
            result.file_markers = find_file_markers(clone_path, paths)
            git_commit_count = None if is_shallow(clone_path) else count_commits(clone_path)
            texts = read_blobs(clone_path, repo_scan.wanted_paths(paths))
            result.signals = repo_scan.scan(paths, texts.get, added=lambda p: first_added(clone_path, p)[1])
        else:
            # --- 1. API-only: commits, tree and file contents -------------
            from .api_collect import collect_commits_api
            commits = collect_commits_api(client, owner, repo, since=shallow_since, until=until,
                                          max_commits=max_commits)
            meta_now = futures["meta"].result()
            entries, truncated = client.get_tree(owner, repo, meta_now.default_branch)
            paths = [e["path"] for e in entries]
            if truncated:
                result.notes.append("file tree truncated by GitHub (very large repo); some files may be missed")
            result.file_markers = _markers_from_paths(paths)
            texts = client.get_blobs(owner, repo, repo_scan.wanted_paths(paths))
            result.signals = repo_scan.scan(paths, texts.get)
            result.notes.append("api mode: no clone, so LOC, changed paths (file-type mix) and "
                                "file first-added dates are not available")
        result.commits = commits
        meta = RepoMetadata(owner=owner, name=repo, full_name=full_name, total_loc=total_loc,
                            commit_count=git_commit_count,
                            commit_count_source="git" if git_commit_count is not None else None)
        result.repo_metadata = meta

        # --- 2. collect the API stage -----------------------------------
        if pool is not None:
            try:
                from .eligibility import _size_tier, check_eligibility
                meta = futures["meta"].result()
                meta.total_loc = total_loc
                meta.size_tier = _size_tier(total_loc)
                if meta.commit_count is None and git_commit_count is not None:
                    meta.commit_count, meta.commit_count_source = git_commit_count, "git"
                result.repo_metadata = meta
                now = datetime.fromisoformat(f"{as_of}T23:59:59+00:00") if as_of else None
                result.eligibility = check_eligibility(meta, now=now)
                result.mode = "full" if mode == "clone" else "api"
                if "prs" in futures:
                    result.pull_requests = futures["prs"].result()
                    gained = _propagate_pr_labels(commits, result.pull_requests)
                    if gained:
                        result.notes.append(f"{gained} merge/squash commit(s) labelled from their agent PR")
                if "issues" in futures:
                    result.issues = futures["issues"].result()
            except LookupError:
                raise
            except Exception as exc:
                if mode == "api":
                    raise
                # A set GITHUB_TOKEN does not guarantee the API works (wrong
                # scope, revoked, placeholder): keep the git-only results.
                result.notes.append(
                    f"GitHub API stage failed ({exc.__class__.__name__}: {exc}); kept git-only "
                    "results. Check that GITHUB_TOKEN is a valid personal access token.")
        else:
            result.notes.append(
                "GITHUB_TOKEN not set: skipped repo metadata, eligibility checks that need the API "
                "(stars/contributors/branch protection), PR and issue collection. "
                "Commit collection via git log ran fully.")

        # --- 3. features and adoption (after PR propagation, so squash
        # commits are known before velocity is ranked) --------------------
        compute_edit_velocity(commits)
        result.feature_vectors = commits_to_feature_vectors(commits)
        result.adoption = summarize_repo(full_name, commits, result.pull_requests, result.file_markers,
                                         signals=result.signals, issues=result.issues)

    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        if mode == "clone" and not keep_clone:
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
    """Write commits/prs/issues/features/file_markers/signals .jsonl and
    summary.json (UTF-8) to out_dir. Returns the paths written.
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
    _write_jsonl("issues.jsonl", result.issues)
    _write_jsonl("features.jsonl", result.feature_vectors)
    _write_jsonl("file_markers.jsonl", result.file_markers)
    _write_jsonl("signals.jsonl", result.signals)

    summary_path = out / "summary.json"
    tmp_path = out / "summary.json.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2, default=str, ensure_ascii=False)
    os.replace(tmp_path, summary_path)
    paths["summary.json"] = str(summary_path)

    return paths
