from afp.collect import collect_commits, compute_edit_velocity
from afp.features import commits_to_feature_vectors


def test_feature_vectors_strip_label_source_fields(synthetic_repo):
    """The whole point of CommitFeatureVector is that it carries no raw
    label-source signal -- only pu_label (derived) survives."""
    commits = collect_commits(synthetic_repo, "test/synthetic")
    compute_edit_velocity(commits)
    fvs = commits_to_feature_vectors(commits)
    for fv in fvs:
        fv_dict = fv.to_dict()
        for forbidden in ("has_trailer", "trailer_text", "is_bot_account",
                          "bot_account_login", "email_domain_match", "email_domain",
                          "pr_marker_match", "pr_marker_text", "agent_brand", "label_source"):
            assert forbidden not in fv_dict


def test_pu_label_matches_commit_label(synthetic_repo):
    commits = collect_commits(synthetic_repo, "test/synthetic")
    compute_edit_velocity(commits)
    fvs = {fv.sha: fv for fv in commits_to_feature_vectors(commits)}
    for c in commits:
        expected = 1 if c.label.label == "agent_labeled" else 0
        assert fvs[c.sha].pu_label == expected


def test_file_type_mix_sums_to_one(synthetic_repo):
    commits = collect_commits(synthetic_repo, "test/synthetic")
    fvs = commits_to_feature_vectors(commits)
    for fv in fvs:
        total = fv.frac_source + fv.frac_docs + fv.frac_config + fv.frac_test
        # each synthetic commit touches exactly one file, so this is exact
        assert abs(total - 1.0) < 1e-9


def test_lockfile_classified_as_config(synthetic_repo):
    commits = collect_commits(synthetic_repo, "test/synthetic")
    fvs = {fv.sha: fv for fv in commits_to_feature_vectors(commits)}
    lock_commit = next(c for c in commits if c.message.startswith("chore: update lockfile"))
    assert fvs[lock_commit.sha].frac_config == 1.0
