"""
Shared pytest fixtures.

synthetic_repo builds a tiny local git repo with known ground truth:
  1. Human commit (plain)
  2. Human commit (plain, 1h later)
  3. Claude Code trailer commit, 500 lines added 60s after commit 2 -> should
     be label=agent_labeled/claude_code/trailer AND edit_velocity_flagged=True
  4. codex-bot e-mail commit (noreply@openai.com), 300 lines, no trailer ->
     label=agent_labeled/codex/email_domain
  5. Lockfile-only commit, 400 lines, 120s after commit 3 -> fast+large but
     must be EXCLUDED from edit-velocity ranking (mechanical_diff)
  6. Human commit the next day (normal pace) -> label=unlabeled

This mirrors the manual repo built and hand-verified during development
(see README "Testing notes").
"""

import os
import subprocess
from pathlib import Path

import pytest


def _run(cmd, cwd, env=None):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True, env=env)


@pytest.fixture(scope="session")
def synthetic_repo(tmp_path_factory) -> str:
    # session-scoped: no test mutates it, and building it per test spawned
    # ~20 git processes each time (slow on Windows)
    repo_dir = tmp_path_factory.mktemp("synthetic") / "synthetic-repo"
    repo_dir.mkdir()
    repo = str(repo_dir)

    _run(["git", "init", "-q"], cwd=repo)
    _run(["git", "config", "user.name", "Human Dev"], cwd=repo)
    _run(["git", "config", "user.email", "human.dev@example.com"], cwd=repo)

    def commit(files: dict, message: str, author_date: str, commit_date: str,
               author_name=None, author_email=None):
        for fname, content in files.items():
            (repo_dir / fname).write_text(content)
            _run(["git", "add", fname], cwd=repo)
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = author_date
        env["GIT_COMMITTER_DATE"] = commit_date
        cmd = ["git", "commit", "-q", "-m", message]
        if author_name and author_email:
            cmd += ["--author", f"{author_name} <{author_email}>"]
            env["GIT_AUTHOR_NAME"] = author_name
            env["GIT_AUTHOR_EMAIL"] = author_email
        _run(cmd, cwd=repo, env=env)

    commit({"app.py": "print('hello')\n"},
           "Initial commit",
           "2026-03-01T10:00:00-00:00", "2026-03-01T10:00:00-00:00")

    commit({"app.py": "print('hello')\nprint('world')\n"},
           "Add world print",
           "2026-03-01T11:00:00-00:00", "2026-03-01T11:00:00-00:00")

    big_content = "\n".join(f"line {i}" for i in range(500)) + "\n"
    commit({"bigfile.py": big_content},
           "Implement large feature\n\nCo-authored-by: Claude <noreply@anthropic.com>",
           "2026-03-01T11:01:00-00:00", "2026-03-01T11:01:00-00:00")

    gen_content = "\n".join(f"x = {i}" for i in range(300)) + "\n"
    commit({"generated.py": gen_content},
           "Generate module",
           "2026-03-01T11:02:00-00:00", "2026-03-01T11:02:00-00:00",
           author_name="codex-bot", author_email="noreply@openai.com")

    lock_content = "\n".join(f'"dep{i}": "1.0.{i}",' for i in range(400)) + "\n"
    commit({"package-lock.json": lock_content},
           "chore: update lockfile",
           "2026-03-01T11:03:00-00:00", "2026-03-01T11:03:00-00:00")

    commit({"NOTES.md": "# notes\n"},
           "docs: add notes",
           "2026-03-02T11:03:00-00:00", "2026-03-02T11:03:00-00:00")

    return repo
