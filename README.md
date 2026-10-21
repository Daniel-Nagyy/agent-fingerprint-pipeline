# Agent Fingerprint Pipeline (AFP)

A repo-to-features pipeline for the thesis *"Identifying Coding-Agent
Fingerprints in Commit Metadata"*. Implements, end to end, the parameters
agreed in `Shared_Corpus_Methodology.tex` and the detection/feature design
from the proposal:

1. **Repo eligibility + metadata** (stars, forks, archived, push recency,
   LOC, branch protection, contributor count) via the GitHub REST API.
2. **Commit collection** via `git clone` + `git log` parsing (the same
   approach Robbes et al. themselves use, not the API).
3. **PR collection** (title/body/branch/labels, reviews, review comments)
   via the GitHub REST API.
4. **Explicit-signal heuristics**: commit trailer / bot account / e-mail
   domain / PR marker detection, with brand resolution and the group's
   label-source priority. Output is always `agent_labeled` or
   `unlabeled` — **never** `human`, since absence of a signal is not
   evidence of human authorship (see the Positive/Unlabeled framing in
   the proposal).
5. **Feature extraction**: message, timing, diff, file-type-mix, merge
   type, and edit velocity — with every raw label-source signal stripped
   out before the vector is handed to a model.
6. **Classifier training stub**: logistic regression / gradient boosting
   baseline, repo-level train/test split, SHAP-ready.

## Install

```bash
cd agent_fingerprint_pipeline
pip install -r requirements.txt
```

## Quick start

```bash
# Git-only mode (no token needed): clones the repo, mines commits, extracts
# features. Skips anything that needs the REST API (eligibility, PR data).
python -m afp.cli run octocat/Hello-World --out ./out/hello_world

# Full mode: set a real GitHub personal access token (public_repo scope is
# enough; no scope at all is needed for public repos on a fine-grained PAT)
export GITHUB_TOKEN=ghp_your_token_here
python -m afp.cli run octocat/Hello-World --out ./out/hello_world

# Restrict to commits after the group's agreed data-collection start date
python -m afp.cli run octocat/Hello-World --out ./out/hello_world --since 2026-02-22

# Train the baseline classifier on the resulting features.jsonl
python -m afp.cli train ./out/hello_world/features.jsonl --out model.joblib
```

Output files per repo (`--out DIR`):
- `commits.jsonl` — one `CommitRecord` per line (raw data + label metadata).
- `prs.jsonl` — one `PullRequestRecord` per line (empty if no token, or `--no-prs`).
- `features.jsonl` — one `CommitFeatureVector` per line (what actually feeds the classifier).
- `summary.json` — run mode, notes, and counts.

## Package layout

```
src/afp/
  schema.py        dataclasses: RepoMetadata, EligibilityResult, LabelSignal,
                    CommitRecord, PullRequestRecord, CommitFeatureVector
  config.py         every group-agreed constant (thresholds, brand/email/bot maps, priority order)
  heuristics.py     explicit-signal detection + label resolution (trailer > bot > email > PR marker)
  collect.py        git clone + git log parsing; edit-velocity computation
  github_client.py  thin REST client, reads GITHUB_TOKEN
  eligibility.py     repo metadata + eligibility verdict
  pr_collect.py     PR-level record building (review depth, rubber-stamp flag, etc.)
  features.py       CommitRecord -> CommitFeatureVector (strips label-source columns)
  pipeline.py       orchestrator: clone -> collect -> (eligibility/PRs) -> features -> export
  classifier.py     baseline training stub (PU-learning-aware, repo-level split)
  cli.py            `run` / `smoke-test-api` / `train` subcommands
tests/              pytest suite (see "Testing notes" below)
```

## Testing notes — what was actually run, and what wasn't

This tool was built inside a sandboxed agentic environment with the
following constraints, which shaped how it was tested:

- **Anonymous `git clone`/`fetch` of any public GitHub repo works** with
  no setup, through the sandbox's own git proxy. So `collect.py`
  (commit collection, edit velocity, the explicit-signal heuristics) was
  **live-tested** against a real cloned repo (`octocat/Hello-World`) —
  see the merge-commit numstat gap it surfaced below — *and* against a
  hand-built synthetic repo with known ground truth (trailer commit,
  bot-email commit, lockfile commit, plain human commits), reproduced in
  `tests/conftest.py`'s `synthetic_repo` fixture.
- **The GitHub REST/GraphQL API does not work from inside this sandbox**
  without attaching the target repo with push-level credentials, which
  would have been disproportionate just to test a read-only client
  against someone else's public demo repo. So `github_client.py` and
  `pr_collect.py` were tested against **mocked HTTP responses** shaped
  like the real documented API (`tests/test_github_client.py`,
  `tests/test_pr_collect.py`), not against the live API.
- **You should smoke-test the API client yourself** with your own token:
  ```bash
  export GITHUB_TOKEN=ghp_your_token_here
  python -m afp.cli smoke-test-api octocat/Hello-World
  ```
  This hits `/repos`, `/languages`, `/pulls`, `/pulls/{n}/reviews` and
  `/pulls/{n}/comments` for real and prints what comes back. If it fails,
  the pipeline's `run` command degrades gracefully to git-only mode
  rather than crashing (see `pipeline.run_pipeline`'s try/except around
  the API stage) — this was itself caught live: this sandbox injects a
  placeholder `GITHUB_TOKEN` that looks set but isn't a real credential,
  and the first version of the orchestrator crashed on it. Worth keeping
  in mind if your own token is wrong/expired — you'll get a note in
  `summary.json` instead of a silent wrong answer.

### Things the live test against `octocat/Hello-World` actually caught

- `git log --numstat` reports **no diff stats for merge commits** by
  default (needs `-m`/`--cc`, which changes the semantics in ways that
  don't match "one row per commit"). The merge commit in that repo comes
  through with `additions=0, deletions=0, files_changed=0`. This is the
  exact squash/merge imprecision flagged as an open methodology question
  — it's now something you can point to concretely rather than just
  suspect.
- `add_delete_ratio = +inf` whenever a commit has zero deletions (common
  for new files) crashes scikit-learn's imputer outright
  (`ValueError: Input X contains infinity`). Fixed in
  `classifier.load_feature_table` by coercing `inf`/`-inf` to `NaN` before
  imputation; regression-tested in `tests/test_classifier.py`.

### Running the tests

```bash
pip install -r requirements.txt pytest
pytest -q
```

31 tests, all passing as of this build: heuristics (label priority, bot
vs. CI-bot distinction, "no signal ≠ human"), commit collection against
the synthetic ground-truth repo, edit-velocity flagging and its
squash/mechanical-diff exclusions, feature stripping, mocked API/PR
parsing, and the classifier's infinity-sanitization fix.

## Known limitations (carried over from the proposal/methodology, not new)

- `merged_via` (merge vs. squash vs. rebase) is **not yet resolved
  automatically** — GitHub's PR API doesn't return it directly; it needs
  a follow-up `compare` call the orchestrator doesn't make yet. Until
  then, `collect.py` only ever sets `merge_type="merge"` for multi-parent
  commits and `None` otherwise — squash-merged PRs collapse into a single
  commit that looks like one big human (or unlabeled) edit, which is the
  exact imprecision the methodology already flags.
- `rubber_stamp_flag`'s 15-minute "fast approval" threshold
  (`config.RUBBER_STAMP_MAX_LATENCY_SECONDS`) is a placeholder — the
  group still needs to agree on the real value.
- The classifier stub has no per-brand holdout, no manual-label
  comparison, and no SHAP wiring yet — it's the scaffold the proposal's
  "Models and Evaluation" section describes, not the finished evaluation.
- Branch protection checks return `None` ("unknown") very often on public
  repos, since GitHub requires push access to read that endpoint for most
  repositories — this matches the methodology's own "recorded as metadata,
  not a filter" treatment of that field.
