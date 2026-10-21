"""
Pipeline orchestrator: wires repo eligibility, commit collection, PR
collection, heuristics and feature extraction into one run per repo.

Two modes:
  - git-only (no GITHUB_TOKEN): clone + commit collection + features only.
    Eligibility checks that need the REST API (stars, LOC, branch
    protection, contributor count) are skipped and reported as such.
  - full (GITHUB_TOKEN set): everything, including PR-level data and a
    proper eligibility verdict.

Output of one run is a dict of lists of plain dicts (JSON-serializable),
written to JSONL files by export_jsonl().
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .collect import clone_repo, collect_commits, compute_edit_velocity
from .features import commits_to_feature_vectors
from .schema import RepoMetadata, EligibilityResult


class PipelineResult:
    def __init__(self):
        self.repo_metadata: Optional[RepoMetadata] = None
        self.eligibility: Optional[EligibilityResult] = None
        self.commits: List[Any] = []
        self.pull_requests: List[Any] = []
        self.feature_vectors: List[Any] = []
        self.mode: str = "git-only"
        self.notes: List[str] = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "notes": self.notes,
            "repo_metadata": self.repo_metadata.to_dict() if self.repo_metadata else None,
            "eligibility": self.eligibility.to_dict() if self.eligibility else None,
            "commit_count": len(self.commits),
            "pr_count": len(self.pull_requests),
            "feature_vector_count": len(self.feature_vectors),
        }


def run_pipeline(owner: str, repo: str, work_dir: Optional[str] = None,
                  branch: Optional[str] = None, shallow_since: Optional[str] = None,
                  collect_prs: bool = True, keep_clone: bool = False) -> PipelineResult:
    full_name = f"{owner}/{repo}"
    result = PipelineResult()

    token = os.environ.get("GITHUB_TOKEN")

    tmp_ctx = None
    if work_dir is None:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="afp_")
        work_dir = tmp_ctx.name
    clone_path = str(Path(work_dir) / repo)

    try:
        # --- 1. clone + commit collection (always runs, no token needed) ---
        clone_url = f"https://github.com/{owner}/{repo}.git"
        clone_repo(clone_url, clone_path, shallow_since=shallow_since)
        commits = collect_commits(clone_path, full_name, branch=branch)
        compute_edit_velocity(commits)
        result.commits = commits

        # --- 2. metadata + eligibility + PRs (need a working token) ---
        if token:
            try:
                from .github_client import GitHubClient
                from .eligibility import build_repo_metadata, check_eligibility

                client = GitHubClient(token=token)
                meta = build_repo_metadata(client, owner, repo)
                meta.commit_count = len(commits)
                result.repo_metadata = meta
                result.eligibility = check_eligibility(meta, commit_count=len(commits))
                result.mode = "full"

                if collect_prs:
                    from .pr_collect import collect_pull_requests
                    result.pull_requests = collect_pull_requests(client, owner, repo)
            except Exception as exc:
                # A GITHUB_TOKEN being *set* doesn't guarantee the API call
                # will actually succeed (wrong scope, revoked, or -- as in
                # this build's own sandbox -- a placeholder value injected
                # by the environment that git-proxies clones but has no
                # real REST API access). Degrade to git-only rather than
                # crashing the whole run.
                result.notes.append(
                    f"GITHUB_TOKEN was set but the REST API call failed ({exc.__class__.__name__}: "
                    f"{exc}); falling back to git-only mode. Commit collection via git log "
                    "still ran fully. Check that GITHUB_TOKEN is a valid personal access token."
                )
        else:
            result.notes.append(
                "GITHUB_TOKEN not set: skipped repo metadata, eligibility checks "
                "that need the REST API (stars/LOC/branch protection/contributors), "
                "and PR-level data collection. Commit collection via git log ran fully."
            )

        # --- 3. features (always runs, derived only from commit data) ---
        result.feature_vectors = commits_to_feature_vectors(commits)

    finally:
        if tmp_ctx is not None and not keep_clone:
            tmp_ctx.cleanup()

    return result


def _records_to_dicts(records: List[Any]) -> List[Dict[str, Any]]:
    out = []
    for r in records:
        out.append(r.to_dict() if hasattr(r, "to_dict") else asdict(r))
    return out


def export_jsonl(result: PipelineResult, out_dir: str) -> Dict[str, str]:
    """Write commits.jsonl, prs.jsonl, features.jsonl, summary.json to
    out_dir. Returns a dict of the paths written."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {}

    def _write_jsonl(name: str, records: List[Any]):
        path = out / name
        with open(path, "w") as f:
            for rec_dict in _records_to_dicts(records):
                f.write(json.dumps(rec_dict, default=str) + "\n")
        paths[name] = str(path)

    _write_jsonl("commits.jsonl", result.commits)
    _write_jsonl("prs.jsonl", result.pull_requests)
    _write_jsonl("features.jsonl", result.feature_vectors)

    summary_path = out / "summary.json"
    with open(summary_path, "w") as f:
        json.dump(result.to_dict(), f, indent=2, default=str)
    paths["summary.json"] = str(summary_path)

    return paths
