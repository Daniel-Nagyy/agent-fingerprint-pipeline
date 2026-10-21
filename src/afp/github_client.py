"""
Thin GitHub REST API client, read-only, token-based.

Reads the token from the GITHUB_TOKEN environment variable. This module
is written as ordinary portable `requests` code -- NOT tied to this
sandbox's git-proxy -- because the GitHub REST/GraphQL API itself needs a
real personal access token, which only the user can provide. The parsing
logic here was tested against mocked/fixture HTTP responses (see
tests/test_github_client.py); it has NOT been exercised against the live
API from inside this sandbox, since that would require attaching a repo
with push-level credentials for no real reason (see README "Testing
notes"). Run `python -m afp.cli smoke-test-api <owner>/<repo>` yourself
with a real GITHUB_TOKEN to confirm it against the live API.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import requests

API_ROOT = "https://api.github.com"
DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3


class GitHubAPIError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: Optional[str] = None, session: Optional[requests.Session] = None):
        self.token = token or os.environ.get("GITHUB_TOKEN")
        self.session = session or requests.Session()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-fingerprint-pipeline",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.session.headers.update(headers)

    # -- low level -----------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            resp = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 5))
                wait = max(0, reset - int(time.time())) + 1
                time.sleep(min(wait, 60))
                continue
            return resp
        if last_exc:
            raise last_exc
        raise GitHubAPIError(f"Exceeded retries fetching {url}")

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

    def get_contributors_count(self, owner: str, repo: str, cap_pages: int = 5) -> int:
        """Approximate contributor count (capped to avoid huge pagination
        on very popular repos -- exact count is not critical, only the
        solo-vs-multi distinction and a rough continuous figure)."""
        contributors = self._get_all_pages(
            f"/repos/{owner}/{repo}/contributors",
            params={"anon": "true"},
            max_pages=cap_pages,
        )
        return len(contributors)

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

    # -- pull requests -----------------------------------------------------

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
