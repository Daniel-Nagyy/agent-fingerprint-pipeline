"""
Commit collection via `git clone` + `git log` parsing.

This mirrors Robbes et al.'s own actual methodology (shallow clone + log
mining) rather than going through the GitHub API for commit history --
it's cheaper, has no rate limit, and works on any public repo with a
plain anonymous `git clone`.

Public entry points:
  - clone_repo(url, dest, shallow_since=None) -> path to the clone
  - collect_commits(repo_path, repo_full_name, branch=None, since=None, until=None)
  - count_loc(repo_path) -> lines in recognised source/config files at HEAD
  - find_file_markers(repo_path) -> agent configuration files present at HEAD

compute_edit_velocity() is a separate pass so it can be unit-tested against
a hand-built list of CommitRecord objects without touching git at all.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, NamedTuple, Optional

from . import config
from .schema import CommitRecord, FileMarker
from .heuristics import resolve_label
from .signatures import file_marker_brand

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

# Never let git stop and wait for a username/password (a deleted or
# private repo answers anonymous clones with an auth challenge), and read
# its output as UTF-8 regardless of the console code page.
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
_GIT_CONFIG = ["-c", "core.quotepath=off", "-c", "i18n.logOutputEncoding=UTF-8",
               "-c", "credential.helper=", "-c", "core.longpaths=true"]

# "Lines of code": source and config files. Docs (.md/.rst/.txt) are left
# out, otherwise an awesome-list README counts as thousands of lines of code.
_CODE_EXTENSIONS = (
    config.FILE_TYPE_GROUPS["source"] | config.FILE_TYPE_GROUPS["config"]
    | {".sh", ".bash", ".ps1", ".css", ".scss", ".less", ".html", ".htm", ".vue",
       ".svelte", ".sql", ".m", ".mm", ".r", ".jl", ".lua", ".pl", ".pm", ".ex",
       ".exs", ".erl", ".hs", ".ml", ".clj", ".dart", ".groovy", ".gradle", ".vim",
       ".el", ".zig", ".nim", ".xml", ".proto", ".tf", ".cmake", ".mk"}
)


class GitCommandError(RuntimeError):
    pass


def _run_git(args: List[str], cwd: str, timeout: int = 120) -> str:
    try:
        result = subprocess.run(
            ["git"] + _GIT_CONFIG + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env={**os.environ, **_GIT_ENV},
        )
    except subprocess.TimeoutExpired as e:
        raise GitCommandError(f"git {' '.join(args)} timed out") from e
    if result.returncode != 0:
        raise GitCommandError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def remove_tree(path: str) -> None:
    """shutil.rmtree that also removes git's read-only pack files on Windows."""
    def _onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if Path(path).exists():
        shutil.rmtree(path, onerror=_onerror)


def clone_repo(clone_url: str, dest_dir: str, shallow_since: Optional[str] = None,
               depth: Optional[int] = None, timeout: int = 900) -> str:
    """Anonymously clone a public repo's default branch without a working
    tree. Returns the path.

    Nothing downstream needs files on disk: commits come from `git log`,
    and LOC and file markers read HEAD's tree from the object store
    (head_tree / count_loc). Skipping the checkout avoids writing tens of
    thousands of files per large repo -- the slowest step of a corpus run,
    especially on Windows, where each written file is virus-scanned.

    `shallow_since` (YYYY-MM-DD) matches the methodology's data-collection
    start date when we only need commits after that point; `depth` is used
    instead for a quick-and-dirty "last N commits" style fetch (useful for
    smoke tests). Passing neither does a full clone.

    A repo with no commit after `shallow_since` makes git refuse the
    shallow clone; it is retried at depth 1 so HEAD's tree (LOC, file
    markers) is still available, and collect_commits' own `since` filter
    then correctly yields zero commits.
    """
    dest = Path(dest_dir)
    remove_tree(str(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)

    base = ["clone", "--single-branch", "--no-tags", "--no-checkout"]
    args = list(base)
    if shallow_since:
        args += ["--shallow-since", shallow_since]
    elif depth:
        args += ["--depth", str(depth)]
    def _clone(clone_args: List[str]) -> None:
        for attempt in range(CLONE_ATTEMPTS):
            remove_tree(str(dest))
            try:
                _run_git(clone_args + [clone_url, str(dest)], cwd=str(dest.parent), timeout=timeout)
                return
            except GitCommandError as exc:
                if attempt == CLONE_ATTEMPTS - 1 or not _is_network_error(str(exc)):
                    raise
                time.sleep(CLONE_RETRY_SECONDS * (attempt + 1))

    try:
        _clone(args)
    except GitCommandError as exc:
        # only git's own "nothing after that date" answer means an empty
        # window; any other failure (network, auth, timeout) must surface,
        # or the repo would silently come back with one commit
        if not shallow_since or "error processing shallow info" not in str(exc).lower():
            raise
        _clone(base + ["--depth", "1"])
    return str(dest)


CLONE_ATTEMPTS = 4
CLONE_RETRY_SECONDS = 10
_NETWORK_ERRORS = ("unable to access", "could not resolve host", "failed to connect", "early eof",
                   "rpc failed", "timed out", "connection reset", "connection was reset",
                   "remote end hung up", "gnutls", "schannel", "http/2 stream")


def _is_network_error(message: str) -> bool:
    low = message.lower()
    return any(s in low for s in _NETWORK_ERRORS)


def is_shallow(repo_path: str) -> bool:
    return _run_git(["rev-parse", "--is-shallow-repository"], cwd=repo_path).strip() == "true"


def count_commits(repo_path: str) -> int:
    """Commits reachable from HEAD in the clone (the full history only
    when the clone is not shallow; see is_shallow)."""
    return int(_run_git(["rev-list", "--count", "HEAD"], cwd=repo_path).strip() or 0)


class TreeEntry(NamedTuple):
    path: str
    oid: str
    size: int


def head_tree(repo_path: str) -> List[TreeEntry]:
    """Every file blob in HEAD's tree, with its object id and size
    (submodules and symlinks excluded). Works on a --no-checkout clone."""
    raw = _run_git(["ls-tree", "-r", "-l", "-z", "HEAD"], cwd=repo_path, timeout=300)
    entries = []
    for rec in raw.split("\0"):
        if not rec or "\t" not in rec:
            continue
        meta, path = rec.split("\t", 1)
        parts = meta.split()
        if len(parts) != 4 or parts[1] != "blob" or parts[0] == "120000" or not parts[3].isdigit():
            continue
        entries.append(TreeEntry(path, parts[2], int(parts[3])))
    return entries


def tracked_files(repo_path: str) -> List[str]:
    return [e.path for e in head_tree(repo_path)]


def _is_code_path(path: str) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    ext = "." + base.rsplit(".", 1)[-1] if "." in base else base
    return ext in _CODE_EXTENSIONS


def count_loc(repo_path: str, entries: Optional[List[TreeEntry]] = None) -> int:
    """Lines in HEAD's source and config files (docs excluded), read from
    the object store with one `git cat-file --batch` stream. Binary files
    (a NUL byte in the first 8 KiB) and files over config.LOC_MAX_FILE_BYTES
    (vendored bundles, data dumps) are skipped. This is what the
    methodology's ">= 5,000 lines of code" threshold is checked against --
    not the languages API, which counts bytes."""
    wanted = [e for e in (entries if entries is not None else head_tree(repo_path))
              if e.size <= config.LOC_MAX_FILE_BYTES and _is_code_path(e.path)]
    if not wanted:
        return 0
    proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=repo_path, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env={**os.environ, **_GIT_ENV})

    def _feed():
        try:
            for e in wanted:
                proc.stdin.write(e.oid.encode("ascii") + b"\n")
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    writer = threading.Thread(target=_feed, daemon=True)
    writer.start()
    total = 0
    try:
        out = proc.stdout
        for _ in wanted:
            header = out.readline()
            if not header:
                break
            parts = header.split()
            if len(parts) < 3 or parts[1] == b"missing":
                continue
            data = out.read(int(parts[2]))
            out.read(1)  # the newline git appends after each object
            if b"\0" in data[:8192]:
                continue
            total += data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    finally:
        writer.join(timeout=30)
        proc.stdout.close()
        proc.wait(timeout=60)
    return total


def read_blobs(repo_path: str, paths: List[str], max_bytes: int = 300_000) -> dict:
    """Text of HEAD:<path> for each path (one `git cat-file --batch`
    call; works on a --no-checkout clone). Missing, binary or oversized
    files are left out."""
    if not paths:
        return {}
    request = "".join(f"HEAD:{p}\n" for p in paths).encode("utf-8")
    proc = subprocess.run(["git", "cat-file", "--batch"], cwd=repo_path, input=request,
                          capture_output=True, timeout=120, env={**os.environ, **_GIT_ENV})
    out, pos, texts = proc.stdout, 0, {}
    for path in paths:
        nl = out.find(b"\n", pos)
        if nl < 0:
            break
        header = out[pos:nl].split()
        pos = nl + 1
        if len(header) < 3 or header[-1] == b"missing":
            continue
        size = int(header[2])
        data = out[pos:pos + size]
        pos += size + 1
        if header[1] == b"blob" and size <= max_bytes and b"\0" not in data[:8192]:
            texts[path] = data.decode("utf-8", errors="replace")
    return texts


def first_added(repo_path: str, path: str) -> tuple:
    """(sha, author date) of the earliest collected commit adding `path`."""
    try:
        out = _run_git(["log", "--diff-filter=A", f"--format=%H{_FIELD_SEP}%aI", "--", path],
                       cwd=repo_path, timeout=120).strip().splitlines()
    except GitCommandError:
        return None, None
    if not out:
        return None, None
    sha, date = out[-1].split(_FIELD_SEP, 1)
    return sha, date


def find_file_markers(repo_path: str, files: Optional[List[str]] = None,
                      max_markers: int = 50) -> List[FileMarker]:
    """Agent configuration files present at HEAD (CLAUDE.md, .cursorrules,
    .github/copilot-instructions.md, ...), each with the earliest commit
    in the collected history that added it -- the repo's adoption date
    for that tool, as far as the clone's history reaches."""
    markers: List[FileMarker] = []
    seen = set()
    for rel in files if files is not None else tracked_files(repo_path):
        hit = file_marker_brand(rel)
        if not hit:
            continue
        marker, brand = hit
        # one row per marker per directory is enough (.claude/ can hold dozens of files)
        key = (marker, rel.rsplit("/", 1)[0] if marker.endswith("/") else rel)
        if key in seen:
            continue
        seen.add(key)
        fm = FileMarker(path=rel, marker=marker, brand=brand)
        try:
            out = _run_git(["log", "--diff-filter=A", f"--format=%H{_FIELD_SEP}%aI", "--", rel],
                           cwd=repo_path, timeout=120).strip().splitlines()
            if out:
                fm.first_added_sha, fm.first_added_at = out[-1].split(_FIELD_SEP, 1)
        except GitCommandError:
            pass
        markers.append(fm)
        if len(markers) >= max_markers:
            break
    return markers


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


_RENAME_BRACE_RE = re.compile(r"\{([^{}]*) => ([^{}]*)\}")


def _normalize_numstat_path(fname: str) -> str:
    """numstat prints renames as `src/{old => new}/f.py` or `old => new`;
    keep the new path."""
    if " => " not in fname:
        return fname
    if "{" in fname:
        return re.sub(r"/{2,}", "/", _RENAME_BRACE_RE.sub(lambda m: m.group(2), fname)).strip("/")
    return fname.split(" => ", 1)[1]


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
            file_paths.append(_normalize_numstat_path(fname))
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
            author_login=None,   # git log has no logins; noreply addresses are resolved via the e-mail
            author_email=ae,
            committer_email=ce,
            author_name=an,
            committer_name=cn,
        )
        commits.append(commit)
    return commits


def collect_commits(repo_path: str, repo_full_name: str,
                    branch: Optional[str] = None, since: Optional[str] = None,
                    until: Optional[str] = None) -> List[CommitRecord]:
    """Parse `git log` into a list of CommitRecord (no edit velocity yet).

    `since`/`until` (YYYY-MM-DD) bound the commit date, so a shallow
    clone's boundary commit, or the single commit of a depth-1 fallback
    clone, never leaks into the window."""
    args = ["log", f"--pretty=format:{_LOG_FORMAT}", "--numstat"]
    if since:
        args.append(f"--since={since}T00:00:00Z")
    if until:
        args.append(f"--until={until}T23:59:59Z")
    if branch:
        args.append(branch)
    raw = _run_git(args, cwd=repo_path, timeout=900)
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


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# an aware sentinel: comparing datetime.min (naive) with git's
# offset-carrying dates raises TypeError as soon as one date is missing
_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


def compute_edit_velocity(commits: List[CommitRecord],
                          percentile: int = config.EDIT_VELOCITY_FLAG_PERCENTILE) -> None:
    """Annotate commits in place with seconds_since_author_prev_commit,
    edit_velocity, edit_velocity_flagged and edit_velocity_excluded_reason.

    Grouped per author e-mail (falls back to author name), sorted by
    author_date. Squash-merge commits and mechanical diffs (lockfiles)
    are excluded from the percentile ranking per the methodology's note,
    but still get an excluded_reason recorded rather than being dropped.
    Merge commits are excluded too: git reports no diff stats for them.
    """
    def _author_key(c: CommitRecord) -> str:
        return (c.author_email or c.author_name or "unknown").lower()

    by_author: dict[str, List[CommitRecord]] = {}
    for c in commits:
        by_author.setdefault(_author_key(c), []).append(c)

    velocities_eligible: List[float] = []

    for author, author_commits in by_author.items():
        author_commits.sort(key=lambda c: _parse_dt(c.author_date) or _EARLIEST)
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
                    elif c.is_merge:
                        excluded_reason = "merge_commit"
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
        c.edit_velocity_flagged = bool(
            c.edit_velocity is not None
            and c.edit_velocity_excluded_reason is None
            and c.edit_velocity >= threshold
        )


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
