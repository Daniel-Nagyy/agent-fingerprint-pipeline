"""
Thin GitHub REST + GraphQL client, read-only, token-based.

Reads the token from the GITHUB_TOKEN environment variable. Built for
corpus-scale runs: one client is shared by every worker thread, and it

  - retries transient failures (500/502/503/504, connection resets,
    timeouts) with exponential backoff;
  - waits out the primary rate limit (X-RateLimit-Remaining hits the
    reserve) and secondary limits (403/429 with Retry-After), logging
    how long it sleeps instead of failing the repo;
  - exposes GraphQL, so repo metadata and PR pages with nested reviews
    cost one request each instead of one-plus-two-per-PR.

Run `python -m afp.cli smoke-test-api <owner>/<repo>` with a real
GITHUB_TOKEN to confirm it against the live API.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

API_ROOT = "https://api.github.com"
GRAPHQL_URL = f"{API_ROOT}/graphql"
DEFAULT_TIMEOUT = 30
MAX_RETRIES = 5
RATE_LIMIT_RESERVE = 25        # stop and wait when fewer requests than this remain
MAX_RATE_LIMIT_WAIT = 3700     # never sleep longer than one reset window
_TRANSIENT_STATUSES = {500, 502, 503, 504}

log = logging.getLogger("afp.github")


class GitHubAPIError(RuntimeError):
    pass


def _last_page(link_header: str) -> Optional[int]:
    m = re.search(r'[?&]page=(\d+)[^>]*>;\s*rel="last"', link_header or "")
    return int(m.group(1)) if m else None


def tokens_from_env() -> List[str]:
    """GITHUB_TOKENS (comma/space separated) for a pool, else GITHUB_TOKEN."""
    pool = os.environ.get("GITHUB_TOKENS", "")
    tokens = [t for t in re.split(r"[,\s]+", pool) if t]
    if not tokens and os.environ.get("GITHUB_TOKEN"):
        tokens = [os.environ["GITHUB_TOKEN"]]
    return tokens


class GitHubClient:
    """Shared, thread-safe client. With several tokens (`tokens=[...]` or
    GITHUB_TOKENS) every request goes out on the token with the most
    budget left in that bucket, and a rate-limited token is parked until
    its reset while the others keep working, so throughput grows with the
    number of tokens."""

    def __init__(self, token: Optional[str] = None, session: Optional[requests.Session] = None,
                 sleep=time.sleep, tokens: Optional[List[str]] = None):
        self.tokens: List[Optional[str]] = list(tokens or ([token] if token else tokens_from_env())) or [None]
        self.token = self.tokens[0]
        self.session = session or requests.Session()
        self._sleep = sleep
        self._lock = threading.Lock()
        self._resume_at: Dict[tuple, float] = {}   # (token index, bucket) -> epoch seconds when it refills
        self._remaining: Dict[tuple, int] = {}
        self.requests_made = 0
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-fingerprint-pipeline",
        })
        # pool sized for a few dozen worker threads sharing this client
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=64)
        if isinstance(self.session, requests.Session):
            self.session.mount("https://", adapter)

    # -- rate limiting ---------------------------------------------------

    def _pick(self, bucket: str) -> int:
        """Index of the token to use next, waiting only if every token in
        the pool is exhausted for this bucket."""
        while True:
            now = time.time()
            with self._lock:
                ready = [i for i in range(len(self.tokens)) if self._resume_at.get((i, bucket), 0.0) <= now]
                if ready:
                    return max(ready, key=lambda i: self._remaining.get((i, bucket), 1 << 30))
                wait = min(self._resume_at.get((i, bucket), 0.0) for i in range(len(self.tokens))) - now
            log.warning("GitHub %s rate limit reached on all %d token(s); waiting %.0f s",
                        bucket, len(self.tokens), wait)
            self._sleep(min(max(wait, 0.0), MAX_RATE_LIMIT_WAIT))
            # we have now waited out the soonest reset: release that token
            with self._lock:
                soonest = min(range(len(self.tokens)), key=lambda i: self._resume_at.get((i, bucket), 0.0))
                self._resume_at[(soonest, bucket)] = 0.0

    def _note_limits(self, index: int, bucket: str, resp: requests.Response) -> None:
        headers = getattr(resp, "headers", None) or {}
        remaining, reset = headers.get("X-RateLimit-Remaining"), headers.get("X-RateLimit-Reset")
        if remaining is None or reset is None:
            return
        try:
            remaining_i, reset_f = int(remaining), float(reset)
        except (TypeError, ValueError):
            return
        with self._lock:
            self._remaining[(index, bucket)] = remaining_i
            if remaining_i < RATE_LIMIT_RESERVE:
                self._resume_at[(index, bucket)] = max(self._resume_at.get((index, bucket), 0.0), reset_f + 1)

    @staticmethod
    def _retry_delay(resp: Optional[requests.Response], attempt: int) -> Optional[float]:
        """Seconds to wait before retrying, or None if the response is final."""
        if resp is None:
            return 2.0 ** attempt
        status = resp.status_code
        headers = getattr(resp, "headers", None) or {}
        if status in _TRANSIENT_STATUSES:
            return 2.0 ** attempt
        if status in (403, 429):
            text = (getattr(resp, "text", "") or "").lower()
            retry_after = headers.get("Retry-After")
            if retry_after is not None:
                try:
                    return float(retry_after) + 1
                except ValueError:
                    return 60.0
            if headers.get("X-RateLimit-Remaining") == "0" or "rate limit" in text:
                try:
                    reset = float(headers.get("X-RateLimit-Reset") or 0)
                except ValueError:
                    reset = 0
                return max(1.0, reset - time.time() + 1) if reset else 60.0
        return None

    def _request(self, method: str, url: str, bucket: str, **kwargs) -> requests.Response:
        last_error = "no response"
        for attempt in range(MAX_RETRIES):
            index = self._pick(bucket)
            token = self.tokens[index]
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            resp: Optional[requests.Response] = None
            try:
                send = self.session.get if method == "GET" else self.session.post
                resp = send(url, timeout=DEFAULT_TIMEOUT, headers=headers, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"{exc.__class__.__name__}: {exc}"
            with self._lock:
                self.requests_made += 1
            if resp is not None:
                self._note_limits(index, bucket, resp)
            delay = self._retry_delay(resp, attempt)
            if delay is None:
                return resp
            if resp is not None:
                last_error = f"HTTP {resp.status_code}"
            rate_limited = resp is not None and resp.status_code in (403, 429)
            if rate_limited and len(self.tokens) > 1:
                # park this token and retry at once on another one
                with self._lock:
                    self._resume_at[(index, bucket)] = time.time() + delay
                continue
            if attempt < MAX_RETRIES - 1:
                log.info("retrying %s %s in %.0f s (%s)", method, url, delay, last_error)
                self._sleep(min(delay, MAX_RATE_LIMIT_WAIT))
        raise GitHubAPIError(f"Exceeded retries fetching {url} ({last_error})")

    # -- low level -----------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        return self._request("GET", url, "core", params=params)

    def _get_all_pages(self, path: str, params: Optional[Dict[str, Any]] = None,
                       max_pages: int = 10) -> List[Dict[str, Any]]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        items: List[Dict[str, Any]] = []
        page = 1
        while page <= max_pages:
            params["page"] = page
            resp = self._get(path, params)
            if resp.status_code != 200:
                raise GitHubAPIError(f"GET {path} -> {resp.status_code}: {resp.text[:300]}")
            batch = resp.json()
            if not isinstance(batch, list):
                return batch  # single-object endpoint
            items.extend(batch)
            if len(batch) < params["per_page"]:
                break
            page += 1
        return items

    def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """POST a GraphQL query and return `data`. NOT_FOUND (a deleted or
        renamed-away repo) raises; RATE_LIMITED is waited out and retried."""
        for attempt in range(MAX_RETRIES):
            resp = self._request("POST", GRAPHQL_URL, "graphql",
                                 json={"query": query, "variables": variables or {}})
            if resp.status_code != 200:
                raise GitHubAPIError(f"GraphQL -> {resp.status_code}: {resp.text[:300]}")
            payload = resp.json()
            errors = payload.get("errors") or []
            if any(e.get("type") == "RATE_LIMITED" for e in errors):
                if attempt < MAX_RETRIES - 1:
                    log.warning("GitHub GraphQL rate limited; waiting 60 s")
                    self._sleep(60)
                    continue
                raise GitHubAPIError("GraphQL rate limited after retries")
            not_found = [e for e in errors if e.get("type") == "NOT_FOUND"]
            if not_found:
                raise GitHubAPIError(f"GraphQL NOT_FOUND: {not_found[0].get('message')}")
            if errors and not payload.get("data"):
                raise GitHubAPIError(f"GraphQL errors: {errors[:2]}")
            return payload.get("data") or {}
        raise GitHubAPIError("GraphQL failed after retries")

    # -- repo metadata ---------------------------------------------------

    def get_repo(self, owner: str, repo: str) -> Dict[str, Any]:
        resp = self._get(f"/repos/{owner}/{repo}")
        if resp.status_code != 200:
            raise GitHubAPIError(f"GET repo {owner}/{repo} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def get_languages(self, owner: str, repo: str) -> Dict[str, int]:
        resp = self._get(f"/repos/{owner}/{repo}/languages")
        if resp.status_code != 200:
            raise GitHubAPIError(f"GET languages {owner}/{repo} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def get_contributors_count(self, owner: str, repo: str) -> int:
        """Contributor count (including anonymous committers) in one call:
        ask for one per page and read the last page number from the Link
        header. GitHub stops listing after roughly 500 identities, so very
        large projects read as ~500 -- enough for solo vs. multi and a
        rough continuous figure."""
        resp = self._get(f"/repos/{owner}/{repo}/contributors", params={"anon": "true", "per_page": 1})
        if resp.status_code == 204:
            return 0
        if resp.status_code != 200:
            raise GitHubAPIError(f"GET contributors {owner}/{repo} -> {resp.status_code}: {resp.text[:300]}")
        last = _last_page((getattr(resp, "headers", None) or {}).get("Link", ""))
        if last is not None:
            return last
        body = resp.json()
        return len(body) if isinstance(body, list) else 0

    def get_branch_protection(self, owner: str, repo: str, branch: str) -> Optional[bool]:
        """Returns True/False, or None if unknown (e.g. 403 on a public repo
        without admin rights -- GitHub requires push access to read this
        endpoint, so "unknown" is the honest answer most of the time)."""
        resp = self._get(f"/repos/{owner}/{repo}/branches/{branch}/protection")
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        # 403 (no permission to view) or anything else -> unknown
        return None

    def get_branch_rules(self, owner: str, repo: str, branch: str) -> Optional[List[Dict[str, Any]]]:
        """Active repository-ruleset rules for a branch. Unlike classic
        protection this endpoint is readable on any public repo."""
        resp = self._get(f"/repos/{owner}/{repo}/rules/branches/{branch}")
        if resp.status_code != 200:
            return None
        body = resp.json()
        return body if isinstance(body, list) else None

    def review_required(self, owner: str, repo: str, branch: str) -> Tuple[Optional[bool], Optional[str]]:
        """(True/False/None, source) for "does merging into `branch` need
        an approving review?". Rulesets are checked first because they are
        publicly readable; classic protection usually answers 403 (None)."""
        rules = self.get_branch_rules(owner, repo, branch)
        if rules:
            for rule in rules:
                if rule.get("type") == "pull_request":
                    params = rule.get("parameters") or {}
                    if (params.get("required_approving_review_count") or 0) > 0 \
                            or params.get("require_code_owner_review"):
                        return True, "rulesets"
        classic = self.get_branch_protection(owner, repo, branch)
        if classic is True:
            return True, "classic"
        if classic is False:
            # 404 from classic protection means "not protected" even to anonymous readers
            return False, "rulesets" if rules is not None else "classic"
        return None, None

    # -- pull requests (REST; corpus runs use pr_collect's GraphQL path) --

    def list_pull_requests(self, owner: str, repo: str, state: str = "closed",
                           max_pages: int = 10) -> List[Dict[str, Any]]:
        return self._get_all_pages(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": state, "sort": "updated", "direction": "desc"},
            max_pages=max_pages,
        )

    def get_pull_request(self, owner: str, repo: str, number: int) -> Dict[str, Any]:
        resp = self._get(f"/repos/{owner}/{repo}/pulls/{number}")
        if resp.status_code != 200:
            raise GitHubAPIError(f"GET PR {owner}/{repo}#{number} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def list_pr_reviews(self, owner: str, repo: str, number: int) -> List[Dict[str, Any]]:
        return self._get_all_pages(f"/repos/{owner}/{repo}/pulls/{number}/reviews")

    def list_pr_review_comments(self, owner: str, repo: str, number: int) -> List[Dict[str, Any]]:
        return self._get_all_pages(f"/repos/{owner}/{repo}/pulls/{number}/comments")

    def rate_limit(self) -> Dict[str, Any]:
        resp = self._get("/rate_limit")
        return resp.json() if resp.status_code == 200 else {}

    # -- API-only mode helpers -------------------------------------------

    def get_tree(self, owner: str, repo: str, ref: str) -> Tuple[List[Dict[str, Any]], bool]:
        """(blob entries of the recursive tree at `ref`, truncated?) in one
        call. GitHub truncates trees above ~100k entries."""
        resp = self._get(f"/repos/{owner}/{repo}/git/trees/{ref}", params={"recursive": "1"})
        if resp.status_code != 200:
            raise GitHubAPIError(f"GET tree {owner}/{repo}@{ref} -> {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        return [e for e in body.get("tree") or [] if e.get("type") == "blob"], bool(body.get("truncated"))

    def get_blobs(self, owner: str, repo: str, paths: List[str], ref: str = "HEAD",
                  max_bytes: int = 300_000) -> Dict[str, str]:
        """Text of several small files in one GraphQL query (aliases)."""
        out: Dict[str, str] = {}
        for start in range(0, len(paths), 40):
            chunk = paths[start:start + 40]
            fields = " ".join(
                f'f{i}: object(expression: {json.dumps(f"{ref}:{p}")}) {{ ... on Blob {{ byteSize isBinary text }} }}'
                for i, p in enumerate(chunk))
            data = self.graphql(f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {fields} }} }}",
                                {"owner": owner, "name": repo})
            repo_data = data.get("repository") or {}
            for i, p in enumerate(chunk):
                blob = repo_data.get(f"f{i}")
                if blob and not blob.get("isBinary") and (blob.get("byteSize") or 0) <= max_bytes \
                        and blob.get("text") is not None:
                    out[p] = blob["text"]
        return out
