"""
Pull-request-level data collection via the GitHub REST API, per the
"Pull request-level feature parameters" table.

Like github_client.py, this is written against the documented GitHub
API schema and unit-tested with mocked responses (tests/test_pr_collect.py);
it has not been run live from inside this sandbox. See README.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from . import config
from .github_client import GitHubClient
from .heuristics import resolve_label
from .schema import PullRequestRecord


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _merged_via(pr_raw: dict) -> Optional[str]:
    """GitHub's PR API doesn't return the merge method directly on the PR
    object; it's inferred from the merge_commit_sha's relationship to the
    head, which needs a follow-up `compare` call. Where that hasn't been
    done, this stays None/"unknown" rather than guessing "merge"."""
    if not pr_raw.get("merged_at"):
        return None
    return pr_raw.get("_merged_via_hint")  # set by the orchestrator if it resolved it via compare


def build_pr_record(client: GitHubClient, owner: str, repo: str, number: int) -> PullRequestRecord:
    pr_raw = client.get_pull_request(owner, repo, number)
    reviews = client.list_pr_reviews(owner, repo, number)
    review_comments = client.list_pr_review_comments(owner, repo, number)

    additions = pr_raw.get("additions", 0) or 0
    deletions = pr_raw.get("deletions", 0) or 0
    pr_size = additions + deletions
    changed_files = pr_raw.get("changed_files", 0) or 0

    review_comment_count = len(review_comments)
    review_depth = (review_comment_count / pr_size) if pr_size > 0 else None

    first_review_at = None
    review_latency_seconds = None
    created_dt = _parse_dt(pr_raw.get("created_at"))
    review_dts = [d for d in (_parse_dt(r.get("submitted_at")) for r in reviews) if d is not None]
    if review_dts:
        first_dt = min(review_dts)
        first_review_at = first_dt.isoformat()
        if created_dt is not None:
            review_latency_seconds = (first_dt - created_dt).total_seconds()

    rubber_stamp_flag = (
        review_comment_count <= config.RUBBER_STAMP_MAX_COMMENTS
        and review_latency_seconds is not None
        and review_latency_seconds <= config.RUBBER_STAMP_MAX_LATENCY_SECONDS
    )

    head = pr_raw.get("head", {}) or {}
    labels = [lbl.get("name", "") for lbl in (pr_raw.get("labels") or [])]

    record = PullRequestRecord(
        repo_full_name=f"{owner}/{repo}",
        number=number,
        title=pr_raw.get("title", "") or "",
        body=pr_raw.get("body"),
        branch_name=head.get("ref"),
        labels=labels,
        author_login=(pr_raw.get("user") or {}).get("login"),
        created_at=pr_raw.get("created_at"),
        merged_at=pr_raw.get("merged_at"),
        merge_commit_sha=pr_raw.get("merge_commit_sha"),
        merged_via=_merged_via(pr_raw),
        additions=additions,
        deletions=deletions,
        changed_files=changed_files,
        pr_size=pr_size,
        review_comment_count=review_comment_count,
        review_depth=review_depth,
        first_review_at=first_review_at,
        review_latency_seconds=review_latency_seconds,
        rubber_stamp_flag=rubber_stamp_flag,
    )
    record.label = resolve_label(
        author_login=record.author_login,
        pr_title=record.title,
        pr_body=record.body or "",
        pr_branch_name=record.branch_name or "",
        pr_labels=record.labels,
    )
    return record


def collect_pull_requests(client: GitHubClient, owner: str, repo: str,
                           state: str = "closed", max_pages: int = 10,
                           max_prs: Optional[int] = None) -> List[PullRequestRecord]:
    listing = client.list_pull_requests(owner, repo, state=state, max_pages=max_pages)
    if max_prs:
        listing = listing[:max_prs]
    records = []
    for pr in listing:
        records.append(build_pr_record(client, owner, repo, pr["number"]))
    return records
