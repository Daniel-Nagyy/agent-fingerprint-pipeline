"""Tests for the corpus-scale layer: API retries and rate limits, the
GraphQL PR path, window filtering, LOC / file markers, the end-to-end
pipeline (git-only, against a local repo) and resumable batches."""

import csv
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from afp import batch, pipeline
from afp.collect import (collect_commits, compute_edit_velocity, count_loc, find_file_markers)
from afp.features import commits_to_feature_vectors
from afp.github_client import GitHubAPIError, GitHubClient
from afp.pr_collect import _infer_merged_via, collect_pull_requests, record_from_graphql
from afp.schema import CommitRecord


def _resp(status=200, json_data=None, headers=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data if json_data is not None else {}
    r.headers = headers or {}
    r.text = text
    return r


# --- GitHub client ---------------------------------------------------------

def test_client_retries_transient_502_then_succeeds():
    sleeps = []
    client = GitHubClient(token="t", sleep=sleeps.append)
    client.session.get = MagicMock(side_effect=[_resp(502), _resp(503), _resp(200, {"full_name": "o/r"})])
    assert client.get_repo("o", "r")["full_name"] == "o/r"
    assert len(sleeps) == 2


def test_client_waits_out_secondary_rate_limit():
    sleeps = []
    client = GitHubClient(token="t", sleep=sleeps.append)
    client.session.get = MagicMock(side_effect=[
        _resp(403, text="You have exceeded a secondary rate limit", headers={"Retry-After": "7"}),
        _resp(200, {"full_name": "o/r"}),
    ])
    client.get_repo("o", "r")
    assert sleeps == [8.0]


def test_client_gives_up_after_retries():
    client = GitHubClient(token="t", sleep=lambda s: None)
    client.session.get = MagicMock(return_value=_resp(502))
    with pytest.raises(GitHubAPIError, match="502"):
        client.get_repo("o", "r")


def test_contributors_count_reads_last_page_from_link_header():
    client = GitHubClient(token="t")
    link = ('<https://api.github.com/repositories/1/contributors?anon=true&per_page=1&page=2>; rel="next", '
            '<https://api.github.com/repositories/1/contributors?anon=true&per_page=1&page=437>; rel="last"')
    client.session.get = MagicMock(return_value=_resp(200, [{"login": "a"}], headers={"Link": link}))
    assert client.get_contributors_count("o", "r") == 437
    client.session.get = MagicMock(return_value=_resp(200, [{"login": "solo"}]))
    assert client.get_contributors_count("o", "r") == 1


def test_review_required_from_public_rulesets():
    client = GitHubClient(token="t")
    rules = [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}]
    client.session.get = MagicMock(return_value=_resp(200, rules))
    assert client.review_required("o", "r", "main") == (True, "rulesets")
    client.session.get = MagicMock(side_effect=[_resp(200, []), _resp(403, text="Forbidden")])
    assert client.review_required("o", "r", "main") == (None, None)


def test_graphql_not_found_raises():
    client = GitHubClient(token="t")
    client.session.post = MagicMock(return_value=_resp(200, {
        "data": {"repository": None},
        "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}]}))
    with pytest.raises(GitHubAPIError, match="NOT_FOUND"):
        client.graphql("query { x }")


# --- GraphQL PRs -----------------------------------------------------------

def _pr_node(number, created, *, author=("someone", "User"), branch="feature", body="",
             merged=None, merge_commit=None, commits=3, reviews=(), comments=(), threads=0):
    return {
        "number": number, "title": f"PR {number}", "body": body, "headRefName": branch,
        "state": "MERGED" if merged else "OPEN", "createdAt": created, "mergedAt": merged,
        "additions": 10, "deletions": 5, "changedFiles": 2,
        "author": {"login": author[0], "__typename": author[1]},
        "labels": {"nodes": []},
        "mergeCommit": merge_commit,
        "commits": {"totalCount": commits},
        "reviewThreads": {"totalCount": threads},
        "reviews": {"nodes": [{"state": s, "submittedAt": t, "author": {"login": l, "__typename": ty}}
                              for (l, ty, s, t) in reviews]},
        "comments": {"nodes": [{"author": {"login": l, "__typename": ty}} for (l, ty) in comments]},
    }


def test_graphql_record_normalises_bot_login_and_ignores_bot_reviews():
    node = _pr_node(
        7, "2026-03-01T10:00:00Z", author=("Copilot", "Bot"), branch="copilot/fix-x",
        merged="2026-03-01T12:00:00Z",
        merge_commit={"oid": "abc", "messageHeadline": "Fix x (#7)", "parents": {"totalCount": 1}},
        reviews=[("coderabbitai", "Bot", "COMMENTED", "2026-03-01T10:01:00Z"),
                 ("Copilot", "Bot", "COMMENTED", "2026-03-01T10:02:00Z"),
                 ("maintainer", "User", "APPROVED", "2026-03-01T10:10:00Z")],
        comments=[("coderabbitai", "Bot")],
    )
    rec = record_from_graphql("o", "r", node)
    assert rec.author_login == "Copilot[bot]"
    assert rec.label.agent_brand == "copilot"
    assert rec.label.label_source == "bot_account"
    assert rec.review_latency_seconds == 600.0          # the maintainer, not the bots
    assert rec.approval_latency_seconds == 600.0
    assert rec.rubber_stamp_flag is True                # 0 threads + approved in 10 min
    assert rec.ai_reviewers == ["CodeRabbit"]
    assert rec.merged_via == "squash"
    assert rec.merge_commit_sha == "abc"


@pytest.mark.parametrize("merge_commit,commits,expected", [
    ({"parents": {"totalCount": 2}, "messageHeadline": "Merge pull request #5"}, 4, "merge"),
    ({"parents": {"totalCount": 1}, "messageHeadline": "Add thing (#5)"}, 4, "squash"),
    ({"parents": {"totalCount": 1}, "messageHeadline": "Add thing"}, 4, "rebase"),
    ({"parents": {"totalCount": 1}, "messageHeadline": "Add thing"}, 1, "squash_or_rebase"),
    (None, 2, None),
])
def test_infer_merged_via(merge_commit, commits, expected):
    assert _infer_merged_via(5, merge_commit, commits) == expected


def test_collect_pull_requests_pages_and_stops_at_window_start():
    client = GitHubClient(token="t")
    page1 = {"repository": {"pullRequests": {
        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
        "nodes": [_pr_node(9, "2026-05-01T00:00:00Z"), _pr_node(8, "2026-04-01T00:00:00Z")]}}}
    page2 = {"repository": {"pullRequests": {
        "pageInfo": {"hasNextPage": True, "endCursor": "c2"},
        "nodes": [_pr_node(7, "2026-03-01T00:00:00Z"), _pr_node(6, "2026-01-15T00:00:00Z")]}}}
    client.graphql = MagicMock(side_effect=[page1, page2])
    prs = collect_pull_requests(client, "o", "r", since="2026-02-22", until="2026-04-30", page_size=2)
    assert [p.number for p in prs] == [8, 7]     # 9 is after `until`, 6 before `since`
    assert client.graphql.call_count == 2         # never asked for page 3


# --- git helpers -----------------------------------------------------------

def _git(cwd, *args, env=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


@pytest.fixture(scope="module")
def marker_repo(tmp_path_factory):
    d = tmp_path_factory.mktemp("markers") / "repo"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.name", "Dev")
    _git(d, "config", "user.email", "dev@example.com")
    env = {**os.environ, "GIT_AUTHOR_DATE": "2026-03-05T10:00:00+00:00",
           "GIT_COMMITTER_DATE": "2026-03-05T10:00:00+00:00"}
    (d / "app.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    (d / "README.md").write_text("# readme\nno trailing newline", encoding="utf-8")
    (d / "logo.png").write_bytes(b"\x89PNG\x00\x00binary\n\n\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "init", env=env)
    env2 = {**env, "GIT_AUTHOR_DATE": "2026-04-01T09:00:00+00:00", "GIT_COMMITTER_DATE": "2026-04-01T09:00:00+00:00"}
    (d / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
    (d / ".claude").mkdir()
    (d / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    (d / ".claude" / "commands.md").write_text("x\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m",
         "Add Claude config\n\n\U0001F916 Generated with [Claude Code](https://claude.com/claude-code)", env=env2)
    return str(d)


def test_count_loc_counts_text_lines_and_skips_binaries(marker_repo):
    # app.py 3 + .claude/settings.json 1; Markdown docs and the PNG are not code
    assert count_loc(marker_repo) == 4


def test_find_file_markers_with_first_added_date(marker_repo):
    markers = {(m.marker, m.path): m for m in find_file_markers(marker_repo)}
    assert set(markers) == {("CLAUDE.md", "CLAUDE.md"), (".claude/", ".claude/commands.md")}
    assert markers[("CLAUDE.md", "CLAUDE.md")].first_added_at.startswith("2026-04-01")


def test_collect_commits_window_and_unicode(marker_repo):
    assert len(collect_commits(marker_repo, "o/r")) == 2
    windowed = collect_commits(marker_repo, "o/r", since="2026-03-10")
    assert len(windowed) == 1
    assert "\U0001F916" in windowed[0].message                    # emoji survives on any code page
    assert windowed[0].label.label_source == "message_marker"
    assert collect_commits(marker_repo, "o/r", until="2026-03-10")[0].message == "init"


def test_trailer_does_not_leak_into_message_features():
    base = "Implement cache layer\n\nAdds an LRU cache."
    with_trailer = base + "\n\nCo-authored-by: Claude <noreply@anthropic.com>"
    plain, agent = (CommitRecord(repo_full_name="o/r", sha=s, message=m) for s, m in
                    (("a", base), ("b", with_trailer)))
    from afp.heuristics import resolve_label
    agent.label = resolve_label(commit_message=with_trailer)
    fv_plain, fv_agent = commits_to_feature_vectors([plain, agent])
    assert fv_agent.pu_label == 1 and fv_plain.pu_label == 0
    assert fv_agent.message_length == fv_plain.message_length
    assert fv_agent.message_line_count == fv_plain.message_line_count


def test_edit_velocity_tolerates_missing_dates():
    commits = [CommitRecord(repo_full_name="o/r", sha="1", author_email="a@x", author_date=None, additions=5),
               CommitRecord(repo_full_name="o/r", sha="2", author_email="a@x",
                            author_date="2026-03-01T10:00:00+01:00", additions=5)]
    compute_edit_velocity(commits)  # used to raise TypeError comparing naive and aware datetimes


def test_clone_retries_network_errors_then_falls_back_to_depth_1(monkeypatch, tmp_path):
    from afp import collect
    calls = []

    def fake_git(args, cwd, timeout=120):
        calls.append(args)
        if len(calls) == 1:
            raise collect.GitCommandError("fatal: unable to access 'https://github.com/o/r.git/': "
                                          "Failed to connect to github.com port 443")
        if "--shallow-since" in args:
            raise collect.GitCommandError("fatal: error processing shallow info: 4")
        return ""
    monkeypatch.setattr(collect, "_run_git", fake_git)
    monkeypatch.setattr(collect, "CLONE_RETRY_SECONDS", 0)
    collect.clone_repo("https://github.com/o/r.git", str(tmp_path / "r"), shallow_since="2026-02-22")
    assert ["--shallow-since" in c for c in calls] == [True, True, False]   # retry, then depth-1 fallback
    assert "--depth" in calls[-1] and "--no-checkout" in calls[-1]


def test_clone_network_failure_is_raised_not_masked_by_depth_1(monkeypatch, tmp_path):
    """Regression: persistent connection failures used to trigger the
    empty-window fallback, so the repo came back with a single commit."""
    from afp import collect
    calls = []

    def fake_git(args, cwd, timeout=120):
        calls.append(args)
        raise collect.GitCommandError("fatal: unable to access 'https://github.com/o/r.git/': "
                                      "Failed to connect to github.com port 443 after 21079 ms")
    monkeypatch.setattr(collect, "_run_git", fake_git)
    monkeypatch.setattr(collect, "CLONE_RETRY_SECONDS", 0)
    with pytest.raises(collect.GitCommandError, match="Failed to connect"):
        collect.clone_repo("https://github.com/o/r.git", str(tmp_path / "r"), shallow_since="2026-02-22")
    assert len(calls) == collect.CLONE_ATTEMPTS and all("--shallow-since" in c for c in calls)


# --- end to end + batch ----------------------------------------------------

@pytest.fixture
def local_clone(monkeypatch, marker_repo):
    """Make run_pipeline clone the local marker repo instead of GitHub."""
    def fake_clone(url, dest, shallow_since=None, depth=None, timeout=900):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        _git(Path(dest).parent, "clone", "-q", "--no-local", marker_repo, dest)
        return dest
    monkeypatch.setattr(pipeline, "clone_repo", fake_clone)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def test_run_pipeline_git_only_end_to_end(local_clone, tmp_path):
    result = pipeline.run_pipeline("o", "r", work_dir=str(tmp_path / "work"))
    assert result.mode == "git-only"
    assert result.repo_metadata.total_loc == 4
    assert result.repo_metadata.commit_count == 2
    assert result.adoption.agent_commits == 1
    assert result.adoption.brands == ["claude_code"]
    assert result.adoption.file_marker_brands == ["claude_code"]
    assert result.adoption.agent_prs_merged == 0              # git-only: no PRs
    assert not (tmp_path / "work" / "o__r").exists()          # clone removed
    paths = pipeline.export_jsonl(result, str(tmp_path / "out"))
    summary = json.loads(Path(paths["summary.json"]).read_text(encoding="utf-8"))
    assert summary["status"] == "ok" and summary["file_marker_count"] == 2


def test_read_repo_list_formats(tmp_path):
    gc = tmp_path / "corpus.csv"
    gc.write_text("﻿id,full_name,stargazers\n1,getify/You-Dont-Know-JS,1\n2,ytdl-org/youtube-dl,2\n"
                  "3,getify/you-dont-know-js,1\n", encoding="utf-8")
    assert batch.read_repo_list(str(gc)) == ["getify/You-Dont-Know-JS", "ytdl-org/youtube-dl"]
    txt = tmp_path / "repos.txt"
    txt.write_text("# pilot\nhttps://github.com/spf13/viper\nmkdocs/mkdocs.git\nnot a repo\n", encoding="utf-8")
    assert batch.read_repo_list(str(txt)) == ["spf13/viper", "mkdocs/mkdocs"]


def test_batch_resumes_and_merges(local_clone, tmp_path, monkeypatch):
    out = tmp_path / "out"
    calls = []
    real = pipeline.run_pipeline

    def counting(owner, repo, **kw):
        calls.append(f"{owner}/{repo}")
        if repo == "broken":
            raise RuntimeError("clone failed")
        return real(owner, repo, **kw)
    monkeypatch.setattr(batch, "run_pipeline", counting)

    report = batch.run_batch(["a/one", "b/two", "c/broken"], str(out), workers=2)
    assert report["repos_ok"] == 2 and report["repos_failed"] == 1
    assert report["brands"] == {"claude_code": 2}
    assert sorted(calls) == ["a/one", "b/two", "c/broken"]

    calls.clear()
    batch.run_batch(["a/one", "b/two", "c/broken"], str(out), workers=2)
    assert calls == []                                       # finished and failed repos skipped
    batch.run_batch(["a/one", "b/two", "c/broken"], str(out), workers=2, retry_failed=True)
    assert calls == ["c/broken"]

    with open(out / "repos.csv", encoding="utf-8-sig") as f:
        rows = {r["full_name"]: r for r in csv.DictReader(f)}
    assert rows["c/broken"]["status"] == "failed" and "clone failed" in rows["c/broken"]["error"]
    assert rows["a/one"]["brands"] == "claude_code"
    with open(out / "adoption_by_brand.csv", encoding="utf-8-sig") as f:
        brand_rows = list(csv.DictReader(f))
    assert brand_rows[0]["brand"] == "claude_code" and brand_rows[0]["repos"] == "2"
    assert sum(1 for _ in open(out / "commits.jsonl", encoding="utf-8")) == 4
    with open(out / "file_markers.csv", encoding="utf-8-sig") as f:
        assert len(list(csv.DictReader(f))) == 4
