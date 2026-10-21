"""
Command-line entry point for the Agent Fingerprint Pipeline.

Usage:
    python -m afp.cli run owner/repo --out ./out/owner_repo
    python -m afp.cli run owner/repo --out ./out/owner_repo --since 2026-02-22
    python -m afp.cli smoke-test-api owner/repo
    python -m afp.cli train features.jsonl --out model.joblib
"""

from __future__ import annotations

import argparse
import os
import sys

from .pipeline import run_pipeline, export_jsonl


def _cmd_run(args: argparse.Namespace) -> int:
    owner, repo = args.repo.split("/", 1)
    result = run_pipeline(
        owner, repo,
        branch=args.branch,
        shallow_since=args.since,
        collect_prs=not args.no_prs,
    )
    paths = export_jsonl(result, args.out)
    print(f"Mode: {result.mode}")
    for note in result.notes:
        print(f"Note: {note}")
    print(f"Commits collected: {len(result.commits)}")
    print(f"PRs collected: {len(result.pull_requests)}")
    print(f"Feature vectors: {len(result.feature_vectors)}")
    if result.eligibility:
        verdict = "ELIGIBLE" if result.eligibility.eligible else "NOT ELIGIBLE"
        print(f"Eligibility: {verdict} ({', '.join(result.eligibility.reasons) or 'no issues'})")
    print("Wrote:")
    for name, path in paths.items():
        print(f"  {name}: {path}")
    return 0


def _cmd_smoke_test_api(args: argparse.Namespace) -> int:
    """A tiny live check of github_client against the real GitHub API.
    Run this yourself with a real GITHUB_TOKEN -- it was not run from
    inside the build sandbox (see README)."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is not set. Export a GitHub personal access token "
              "(no special scopes needed for public repos) and re-run.", file=sys.stderr)
        return 1

    from .github_client import GitHubClient
    owner, repo = args.repo.split("/", 1)
    client = GitHubClient(token=token)

    print(f"GET /repos/{owner}/{repo} ...")
    raw = client.get_repo(owner, repo)
    print(f"  full_name={raw.get('full_name')} stars={raw.get('stargazers_count')} "
          f"fork={raw.get('fork')} archived={raw.get('archived')} "
          f"pushed_at={raw.get('pushed_at')} language={raw.get('language')}")

    print(f"GET /repos/{owner}/{repo}/languages ...")
    langs = client.get_languages(owner, repo)
    print(f"  {langs}")

    print(f"GET /repos/{owner}/{repo}/pulls?state=closed (first page) ...")
    prs = client.list_pull_requests(owner, repo, max_pages=1)
    print(f"  fetched {len(prs)} PRs")
    if prs:
        print(f"GET pulls/{prs[0]['number']}/reviews and /comments ...")
        reviews = client.list_pr_reviews(owner, repo, prs[0]["number"])
        comments = client.list_pr_review_comments(owner, repo, prs[0]["number"])
        print(f"  reviews={len(reviews)} review_comments={len(comments)}")

    print("Smoke test passed: live API responses parsed without error.")
    return 0


def _cmd_train(args: argparse.Namespace) -> int:
    from .classifier import train_from_jsonl
    train_from_jsonl(args.features_jsonl, args.out)
    print(f"Model written to {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="afp", description="Agent Fingerprint Pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the full pipeline on one repo")
    p_run.add_argument("repo", help="owner/repo, e.g. octocat/Hello-World")
    p_run.add_argument("--out", required=True, help="output directory for JSONL/summary files")
    p_run.add_argument("--branch", default=None, help="branch to collect commits from (default: HEAD)")
    p_run.add_argument("--since", default=None, help="shallow-clone cutoff date YYYY-MM-DD "
                                                       "(use the group's data-collection start date)")
    p_run.add_argument("--no-prs", action="store_true", help="skip PR-level collection even if a token is set")
    p_run.set_defaults(func=_cmd_run)

    p_smoke = sub.add_parser("smoke-test-api", help="Live-check github_client against the real API (needs GITHUB_TOKEN)")
    p_smoke.add_argument("repo", help="owner/repo")
    p_smoke.set_defaults(func=_cmd_smoke_test_api)

    p_train = sub.add_parser("train", help="Train the baseline classifier from a features.jsonl file")
    p_train.add_argument("features_jsonl", help="path to a features.jsonl produced by `run`")
    p_train.add_argument("--out", default="model.joblib", help="where to write the trained model")
    p_train.set_defaults(func=_cmd_train)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
