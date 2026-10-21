from afp import config
from afp.collect import collect_commits, compute_edit_velocity


def test_collect_commits_count_and_order(synthetic_repo):
    commits = collect_commits(synthetic_repo, "test/synthetic")
    assert len(commits) == 6
    # git log is newest-first
    assert commits[0].message.startswith("docs: add notes")
    assert commits[-1].message.startswith("Initial commit")


def test_trailer_commit_labeled_and_sized_correctly(synthetic_repo):
    commits = collect_commits(synthetic_repo, "test/synthetic")
    big = next(c for c in commits if c.message.startswith("Implement large feature"))
    assert big.additions == 500
    assert big.deletions == 0
    assert big.label.label == config.LABEL_AGENT
    assert big.label.agent_brand == "claude_code"
    assert big.label.label_source == "trailer"


def test_codex_bot_email_commit_labeled(synthetic_repo):
    commits = collect_commits(synthetic_repo, "test/synthetic")
    gen = next(c for c in commits if c.message.startswith("Generate module"))
    assert gen.label.label == config.LABEL_AGENT
    assert gen.label.agent_brand == "codex"
    assert gen.label.label_source == "email_domain"


def test_plain_human_commits_unlabeled(synthetic_repo):
    commits = collect_commits(synthetic_repo, "test/synthetic")
    initial = next(c for c in commits if c.message.startswith("Initial commit"))
    assert initial.label.label == config.LABEL_UNLABELED


def test_edit_velocity_flags_fast_large_trailer_commit(synthetic_repo):
    commits = collect_commits(synthetic_repo, "test/synthetic")
    compute_edit_velocity(commits)
    big = next(c for c in commits if c.message.startswith("Implement large feature"))
    assert big.seconds_since_author_prev_commit == 60.0
    assert big.edit_velocity == 500 / 60.0
    assert big.edit_velocity_flagged is True
    assert big.edit_velocity_excluded_reason is None


def test_edit_velocity_excludes_mechanical_lockfile_diff(synthetic_repo):
    """The lockfile commit is big and fast (400 lines / 120s) -- faster per
    line, even, than the flagged trailer commit -- but must be excluded
    from the ranking as a mechanical diff, per the methodology's note."""
    commits = collect_commits(synthetic_repo, "test/synthetic")
    compute_edit_velocity(commits)
    lock = next(c for c in commits if c.message.startswith("chore: update lockfile"))
    assert lock.edit_velocity_excluded_reason == "mechanical_diff"
    assert lock.edit_velocity_flagged is False


def test_edit_velocity_none_for_authors_first_commit(synthetic_repo):
    commits = collect_commits(synthetic_repo, "test/synthetic")
    compute_edit_velocity(commits)
    initial = next(c for c in commits if c.message.startswith("Initial commit"))
    assert initial.seconds_since_author_prev_commit is None
    assert initial.edit_velocity is None

    # codex-bot has only one commit in this repo -> also no prior-commit delta
    gen = next(c for c in commits if c.message.startswith("Generate module"))
    assert gen.seconds_since_author_prev_commit is None
