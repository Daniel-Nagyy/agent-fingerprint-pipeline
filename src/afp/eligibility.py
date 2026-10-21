"""
Repo eligibility + metadata checks, per the "Repo eligibility and time
window" table in Shared_Corpus_Methodology.tex.

build_repo_metadata() turns a raw `get_repo` response (plus a couple of
follow-up calls) into a RepoMetadata record. check_eligibility() applies
the group's thresholds and returns a pass/fail with reasons, so a
rejected repo's reasons can be logged rather than silently dropped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import config
from .github_client import GitHubClient
from .schema import RepoMetadata, EligibilityResult


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


def _size_tier(total_loc: Optional[int]) -> Optional[str]:
    if total_loc is None:
        return None
    for tier, (lo, hi) in config.SIZE_TIER_BOUNDARIES.items():
        if total_loc >= lo and (hi is None or total_loc < hi):
            return tier
    return None


def build_repo_metadata(client: GitHubClient, owner: str, repo: str,
                         fetch_contributors: bool = True,
                         fetch_branch_protection: bool = True) -> RepoMetadata:
    raw = client.get_repo(owner, repo)
    languages = client.get_languages(owner, repo)
    total_loc = sum(languages.values()) if languages else None  # bytes, used as a size proxy

    meta = RepoMetadata(
        owner=owner,
        name=repo,
        full_name=raw.get("full_name", f"{owner}/{repo}"),
        stars=raw.get("stargazers_count", 0) or 0,
        is_fork=bool(raw.get("fork", False)),
        is_archived=bool(raw.get("archived", False)),
        pushed_at=raw.get("pushed_at"),
        created_at=raw.get("created_at"),
        primary_language=raw.get("language"),
        languages=languages or {},
        total_loc=total_loc,
        commit_count=None,  # filled in from collect.collect_commits() by the orchestrator
        default_branch=raw.get("default_branch", "main"),
    )
    meta.stack_tag = config.LANGUAGE_TO_STACK_TAG.get(meta.primary_language or "", meta.primary_language)
    meta.size_tier = _size_tier(meta.total_loc)

    if fetch_contributors:
        try:
            meta.contributor_count = client.get_contributors_count(owner, repo)
        except Exception:
            meta.contributor_count = None

    if fetch_branch_protection:
        try:
            meta.branch_protection = client.get_branch_protection(owner, repo, meta.default_branch)
        except Exception:
            meta.branch_protection = None

    return meta


def check_eligibility(meta: RepoMetadata, commit_count: Optional[int] = None,
                       now: Optional[datetime] = None) -> EligibilityResult:
    """Apply the group's eligibility thresholds. `commit_count` can be
    supplied separately (e.g. from collect.collect_commits) since the
    REST API does not give an exact commit count directly."""
    now = now or datetime.now(timezone.utc)
    reasons = []

    if meta.is_fork and not config.ALLOW_FORKS:
        reasons.append("is a fork")

    if meta.is_archived:
        reasons.append("is archived")

    pushed_dt = _parse_dt(meta.pushed_at)
    if pushed_dt is None:
        reasons.append("pushed_at missing/unparseable")
    else:
        age_days = (now - pushed_dt).days
        if age_days > config.PUSH_RECENCY_MONTHS * 30:
            reasons.append(
                f"not pushed in last {config.PUSH_RECENCY_MONTHS} months "
                f"(last push {age_days} days ago)"
            )

    if meta.stars < config.MIN_STARS:
        reasons.append(f"stars {meta.stars} < minimum {config.MIN_STARS}")

    if meta.total_loc is not None and meta.total_loc < config.MIN_LOC:
        reasons.append(f"total_loc {meta.total_loc} < minimum {config.MIN_LOC}")

    effective_commit_count = commit_count if commit_count is not None else meta.commit_count
    if effective_commit_count is not None and effective_commit_count < config.MIN_COMMITS:
        reasons.append(f"commit_count {effective_commit_count} < minimum {config.MIN_COMMITS}")
    elif effective_commit_count is None:
        reasons.append("commit_count unknown (not yet collected)")

    return EligibilityResult(
        full_name=meta.full_name,
        eligible=(len(reasons) == 0),
        reasons=reasons,
        checked_at=now.isoformat(),
    )
