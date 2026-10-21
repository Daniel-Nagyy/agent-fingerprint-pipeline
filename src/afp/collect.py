"""
Commit collection via `git clone` + `git log` parsing.

This mirrors Robbes et al.'s own actual methodology (shallow clone + log
mining) rather than going through the GitHub API for commit history --
it's cheaper, has no rate limit, and works on any public repo with a
plain anonymous `git clone`.

Two public entry points:
  - clone_repo(url, dest, shallow_since=None) -> path to the clone
  - collect_commits(repo_path, repo_full_name, branch=None) -> List[CommitRecord]

compute_edit_velocity() is a separate pass so it can be unit-tested against
a hand-built list of CommitRecord objects without touching git at all.
"""

from __future__ import annotations

import os
import re
import shutil
import statistics
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import config
from .schema import CommitRecord
from .heuristics import resolve_label

# Field/record separators chosen to be control characters that will not
# plausibly appear in a commit message.
_REC_START = "\x02"
_REC_END = "\x03"
_FIELD_SEP = "\x1f"

_LOG_FORMAT = (
    f"{_REC_START}%H{_FIELD_SEP}%P{_FIELD_SEP}%an{_FIELD_SEP}%ae{_FIELD_SEP}"
    f"%cn{_FIELD_SEP}%ce{_FIELD_SEP}%aI{_FIELD_SEP}%cI{_FIELD_SEP}%s{_FIELD_SEP}%b{_REC_END}"
)

_LOCK_FILE_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "gemfile.lock", "cargo.lock", "composer.lock", "pipfile.lock",
}


class GitCommandError(RuntimeError):
    pass


def _run_git(args: List[str], cwd: str, timeout: int = 120) -> str:
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise GitCommandError(f"git {' '.join(args)} timed out") from e
    if result.returncode != 0:
        raise GitCommandError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def clone_repo(clone_url: str, dest_dir: str, shallow_since: Optional[str] = None,
                depth: Optional[int] = None) -> str:
    """Anonymously clone a public repo. Returns the local path.

    `shallow_since` (YYYY-MM-DD) matches the methodology's data-collection
    start date when we only need commits after that point; `depth` is used
    instead for a quick-and-dirty "last N commits" style fetch (useful for
    smoke tests). Passing neither does a full clone.
    """
    dest = Path(dest_dir)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    args = ["clone"]
    if shallow_since:
        args += ["--shallow-since", shallow_since]
    elif depth:
        args += ["--depth", str(depth)]
    args += [clone_url, str(dest)]

    _run_git(args, cwd=str(dest.parent), timeout=600)
    return str(dest)


def _classify_file(path: str) -> str:
    """Return one of 'source' / 'docs' / 'config' / 'test' / 'other'."""
    lower = path.lower()
    basename = lower.rsplit("/", 1)[-1]

    # test paths: path heuristic takes priority over extension
    if "/test" in lower or lower.startswith("test") or "/tests/" in lower or "_test." in basename or "test_" in basename:
        return "test"

    if basename in _LOCK_FILE_NAMES:
        return "config"

    ext = ""
    if "." in basename:
        ext = "." + basename.rsplit(".", 1)[-1]

    if basename == "dockerfile" or ext in config.FILE_TYPE_GROUPS["config"]:
        return "config"
    if ext in config.FILE_TYPE_GROUPS["docs"]:
        return "docs"
    if ext in config.FILE_TYPE_GROUPS["source"]:
        return "source"
    return "other"


def _parse_log_output(raw: str, repo_full_name: str) -> List[CommitRecord]:
    commits: List[CommitRecord] = []
    # split on REC_START, each chunk is "<header>%REC_END%\n<numstat lines>"
    chunks = raw.split(_REC_START)
    for chunk in chunks:
        if not chunk.strip():
            continue
        if _REC_END not in chunk:
            continue
        header, rest = chunk.split(_REC_END, 1)
        fields = header.split(_FIELD_SEP)
        if len(fields) < 9:
            continue
        sha, parents_str, an, ae, cn, ce, a_date, c_date, subject = fields[:9]
        body = fields[9] if len(fields) > 9 else ""
        message = subject
        if body.strip():
            message = subject + "\n" + body

        parents = parents_str.split() if parents_str.strip() else []
        is_merge = len(parents) > 1

        additions = 0
        deletions = 0
        file_paths: List[str] = []
        for line in rest.strip("\n").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            add_s, del_s, fname = parts
            file_paths.append(fname)
            # binary files report "-" for add/del
            if add_s.isdigit():
                additions += int(add_s)
            if del_s.isdigit():
                deletions += int(del_s)

        commit = CommitRecord(
            repo_full_name=repo_full_name,
            sha=sha,
            author_name=an or None,
            author_email=ae or None,
            committer_name=cn or None,
            committer_email=ce or None,
            author_date=a_date or None,
            commit_date=c_date or None,
            message=message,
            parents=parents,
            is_merge=is_merge,
            merge_type="merge" if is_merge else None,
            additions=additions,
            deletions=deletions,
            files_changed=len(file_paths),
            file_paths=file_paths,
        )
        commit.label = resolve_label(
            commit_message=message,
            author_login=None,   # not available from git log alone; filled in later if PR data joins it
            author_email=ae,
            committer_email=ce,
        )
        commits.append(commit)
    return commits


def collect_commits(repo_path: str, repo_full_name: str,
                     branch: Optional[str] = None) -> List[CommitRecord]:
    """Parse `git log` into a list of CommitRecord (no edit velocity yet)."""
    args = ["log", f"--pretty=format:{_LOG_FORMAT}", "--numstat"]
    if branch:
        args.insert(1, branch)
    raw = _run_git(args, cwd=repo_path, timeout=300)
    return _parse_log_output(raw, repo_full_name)


def _is_mechanical_diff(commit: CommitRecord) -> bool:
    if not commit.file_paths:
        return False
    basenames = [p.lower().rsplit("/", 1)[-1] for p in commit.file_paths]
    if all(b in _LOCK_FILE_NAMES for b in basenames):
        return True
    # bulk reformatting heuristic: single file, enormous line count relative
    # to typical commits is handled by the percentile flag itself; here we
    # only catch the "all files are the same generated artifact" case.
    return False


def compute_edit_velocity(commits: List[CommitRecord],
                           percentile: int = config.EDIT_VELOCITY_FLAG_PERCENTILE) -> None:
    """Annotate commits in place with seconds_since_author_prev_commit,
    edit_velocity, edit_velocity_flagged and edit_velocity_excluded_reason.

    Grouped per author e-mail (falls back to author name), sorted by
    author_date. Squash-merge commits and mechanical diffs (lockfiles)
    are excluded from the percentile ranking per the methodology's note,
    but still get an excluded_reason recorded rather than being dropped.
    """
    def _author_key(c: CommitRecord) -> str:
        return (c.author_email or c.author_name or "unknown").lower()

    def _parse_dt(s: Optional[str]) -> Optional[datetime]:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None

    by_author: dict[str, List[CommitRecord]] = {}
    for c in commits:
        by_author.setdefault(_author_key(c), []).append(c)

    velocities_eligible: List[float] = []

    for author, author_commits in by_author.items():
        author_commits.sort(key=lambda c: _parse_dt(c.author_date) or datetime.min)
        prev_dt: Optional[datetime] = None
        for c in author_commits:
            dt = _parse_dt(c.author_date)
            if prev_dt is not None and dt is not None:
                delta = (dt - prev_dt).total_seconds()
                if delta > 0:
                    c.seconds_since_author_prev_commit = delta
                    c.edit_velocity = (c.additions + c.deletions) / delta

                    excluded_reason = None
                    if c.merge_type == "squash":
                        excluded_reason = "squash_merge"
                    elif _is_mechanical_diff(c):
                        excluded_reason = "mechanical_diff"
                    c.edit_velocity_excluded_reason = excluded_reason

                    if excluded_reason is None:
                        velocities_eligible.append(c.edit_velocity)
            if dt is not None:
                prev_dt = dt

    if len(velocities_eligible) < 2:
        return

    threshold = _percentile(velocities_eligible, percentile)
    for c in commits:
        if (c.edit_velocity is not None
                and c.edit_velocity_excluded_reason is None
                and c.edit_velocity >= threshold):
            c.edit_velocity_flagged = True


def _percentile(values: List[float], pct: int) -> float:
    if not values:
        return float("inf")
    data = sorted(values)
    k = (len(data) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(data) - 1)
    if f == c:
        return data[f]
    return data[f] + (data[c] - data[f]) * (k - f)
