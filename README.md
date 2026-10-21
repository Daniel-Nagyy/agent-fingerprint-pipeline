# Agent Fingerprint Pipeline (AFP)

A repo-to-features pipeline for the thesis *"Identifying Coding-Agent
Fingerprints in Commit Metadata"*. It implements the parameters agreed in
the group's shared corpus methodology, from repo eligibility to a
classifier-ready feature table, and runs on a single repo or a whole
corpus (for example a frozen [gitcorpus](https://github.com/Thabettt/gitcorpus)
export) in parallel, resumably.

1. **Repo eligibility + metadata**: stars, fork/archived, push recency,
   lines of code (counted from the clone), full commit count, contributor
   count, and "review required" on the default branch.
2. **Commit collection** via `git clone` + `git log` (the approach
   Robbes et al. use), bounded to a date window.
3. **PR collection** via GraphQL: title/body/branch/labels, author,
   merge method, reviews, review threads, AI reviewers.
4. **Explicit-signal heuristics** for **29 named agent brands** (plus
   `unknown`) across six
   channels: commit trailers, agent-written message markers, bot
   accounts, agent e-mail addresses, PR markers (branch prefix / label /
   body), and repo file markers (`CLAUDE.md`, `.cursorrules`,
   `.github/copilot-instructions.md`, ...). Labels follow the group's
   source priority, and the output is always `agent_labeled` or
   `unlabeled`, **never** `human`.
5. **Feature extraction**: message, timing, diff, file-type mix, merge
   type and edit velocity, with every label-source signal stripped
   *before* the features are computed.
6. **Adoption roll-ups** per repo and per corpus.
7. **Classifier baseline**: logistic regression / gradient boosting,
   repo-level train/test split, PU-learning-aware.

## Install

```bash
git clone https://github.com/<you>/agent-fingerprint-pipeline
cd agent-fingerprint-pipeline
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Needs Python 3.9+ and `git` on the PATH. Commands below assume
`PYTHONPATH=src` (or run them from `src/`).

```bash
export PYTHONPATH=src                 # PowerShell: $env:PYTHONPATH = 'src'
export GITHUB_TOKEN=github_pat_...     # PowerShell: $env:GITHUB_TOKEN = '...'
```

A fine-grained token with **no scopes** is enough for public repos.
Without a token the pipeline runs in git-only mode (commits, file markers,
LOC and features; no PRs, stars, contributors or eligibility verdict).

## Quick start

```bash
# One repo, commits from the data-collection start date onward
python -m afp.cli run spf13/viper --out ./out/viper --since 2026-02-22

# A whole corpus: a gitcorpus corpus.csv (any CSV with a full_name column)
# or a text file with one owner/repo per line
python -m afp.cli batch corpus.csv --out ./out/corpus --since 2026-02-22 --workers 8

# Pilot on the first 20 repos, then run the rest with the same command
python -m afp.cli batch corpus.csv --out ./out/corpus --since 2026-02-22 --limit 20

# Re-print the corpus summary / rebuild the merged files
python -m afp.cli report ./out/corpus

# Train the baseline classifier on every repo's commits
python -m afp.cli train ./out/corpus/features.jsonl --out model.joblib --model gboost

# Check your token and the API layer
python -m afp.cli smoke-test-api readthedocs/sphinx_rtd_theme
```

Window options (`run` and `batch`):

| Option | Meaning |
|---|---|
| `--since YYYY-MM-DD` | First day of the window. The clone is shallow from this date, which keeps large repos cheap. |
| `--until YYYY-MM-DD` | Last day of the window (commits and PRs). |
| `--as-of YYYY-MM-DD` | Date the "pushed in the last 12 months" rule is measured from (default: today). |
| `--max-prs N` | Newest PRs per repo (default 300; `0` = every PR in the window). |
| `--no-prs` | Skip PR collection. |

Batch-only: `--workers N` (parallel repos, default 4), `--retry-failed`,
`--limit N`, `--keep-clones`, `--work-dir DIR`.

### Running on a large corpus

- **Resumable.** Each repo writes `repos/<owner>__<repo>/summary.json`
  last. Re-running the same command skips finished repos (and failed ones
  unless `--retry-failed`), so Ctrl+C, a crash or a reboot loses at most
  the repos in flight.
- **One bad repo never stops the run.** Deleted, renamed, private or huge
  repos are recorded as `failed` with the error and the batch continues.
- **Rate limits are handled.** All workers share one client that retries
  500/502/503/504 and network errors with backoff, and waits for the
  primary and secondary rate limits to reset (logged with `-v`).
- **Disk stays small.** Clones are deleted as soon as a repo is done, so
  only about `--workers` clones exist at once.
- **API cost per repo:** 1 GraphQL query for metadata, 1 GraphQL query per
  50 PRs (each about 3 points), and 3 REST calls. With one token that is
  roughly 1,500 repos/hour on the REST budget; in practice cloning is the
  bottleneck. A live run on a 185-repo gitcorpus corpus is described
  below.

## Output

Per repo (`run --out DIR`, or `batch`'s `repos/<owner>__<repo>/`):

| File | Contents |
|---|---|
| `commits.jsonl` | One `CommitRecord` per commit in the window: raw data + full label evidence. |
| `prs.jsonl` | One `PullRequestRecord` per PR (empty without a token or with `--no-prs`). |
| `features.jsonl` | One `CommitFeatureVector` per commit: what feeds the classifier. |
| `file_markers.jsonl` | Agent configuration files present at HEAD, with the commit that first added each. |
| `summary.json` | Status, mode, window, notes, repo metadata, eligibility, adoption roll-up. |

Corpus level (`batch --out DIR`, rebuilt by `report`):

| File | Contents |
|---|---|
| `repos.csv` | One row per repo: metadata, eligibility verdict + reasons, adoption (brands, agent commit/PR shares, first agent commit, AI reviewers). |
| `adoption_by_brand.csv` | Repos / agent commits / agent PRs per brand, for all repos and for eligible repos only. |
| `file_markers.csv` | Every agent config file found, with its first-added date. |
| `commits.jsonl`, `prs.jsonl`, `features.jsonl` | All repos concatenated (`features.jsonl` is the training set). |
| `corpus_summary.json` | Headline numbers. |

CSV files are UTF-8 with a BOM so Excel opens them correctly.

## Detection

Signatures live in `src/afp/signatures.py`. The roster follows Robbes et
al.'s validated heuristic set (file markers, commit trailers, branch
names, PR labels), extended with the bot accounts, e-mail addresses and
body markers the agents write themselves. The five methodology brands
(`claude_code`, `copilot`, `codex`, `gemini`, `cursor`) are reported as
`primary_brands`. Everything else (Aider, Devin, Jules, Junie, OpenHands,
Windsurf, Cline, Amazon Q, ...) is still detected and reported, and generic
traces (`AGENTS.md`, an `ai-generated` label) map to `unknown`.

**Every pattern is anchored** to where the agent writes it:

| Channel | Matches | Does not match |
|---|---|---|
| Trailer | `Co-authored-by: Cursor <cursoragent@cursor.com>`, `Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>`, a noreply address of an agent account | `Co-authored-by: Claude Martin <claude.martin@example.com>` |
| Message marker | `🤖 Generated with [Claude Code]`, a `chatgpt.com/codex/tasks/` link, an `aider: ` subject | "Move cursor to the next line" |
| Bot account / e-mail | `copilot-swe-agent[bot]`, `google-labs-jules[bot]`, `noreply@anthropic.com` | `osman-haider`, `someone@openai.com` |
| PR marker | branch `codex/...`, `claude/...`; label `codex`; body marker | title "Fix cursor jumps", branch `docs/copilot` |
| File marker | `CLAUDE.md`, `.cursor/`, `.github/copilot-instructions.md`, `AGENTS.md` | `src/cursor.py`, `docs/claude.md` |

Free-text matching ("cursor", "claude" anywhere in a message, a login
merely *containing* "aider") was the main false-positive source in an
earlier detection run on the same corpus. `tests/test_signatures.py`
keeps those real cases as regression tests.

**Label-source priority:** trailer > message marker > bot account >
e-mail > PR marker. When an agent PR is merged, its label is propagated to
the squash or merge commit GitHub created, as the lowest-priority source
(`propagated_from_pr`). Every brand any signal named is kept in
`label.brands`.

**AI reviewers** (CodeRabbit, Copilot review, Gemini Code Assist, ...) are
recorded per PR in `ai_reviewers`. They are review adoption, not
authorship, and never label a PR or commit.

## Methodology notes and limitations

- **Lines of code** are counted from the clone: lines in tracked source
  and config files at HEAD (docs, binaries and files over 2 MB excluded).
  The languages API reports bytes, not lines.
- **Commit count** for the `>= 100` rule is the full default-branch
  history from GraphQL, not the shallow clone's window.
- **Review required / branch protection** comes from the public rulesets
  endpoint first. Classic protection is usually unreadable without push
  access, so `None` ("unknown") is still common.
- **Review depth** uses review *threads* (one per inline comment chain)
  from GraphQL. Exact comment counts cost about 6× more GraphQL points per
  page. `build_pr_record` (REST) still counts individual comments for spot
  checks.
- **Review latency** ignores reviews by the PR author and by bots. The
  **rubber-stamp flag** is zero review threads plus an *approval* within
  `RUBBER_STAMP_MAX_LATENCY_SECONDS` (15 min placeholder; group to agree).
- **Merge method** is inferred from the merge commit: two parents means
  merge; one parent with GitHub's default `(#N)` title means squash; one
  parent otherwise means rebase; a one-commit PR is `squash_or_rebase`.
  Squash commits are excluded from edit-velocity ranking, as the
  methodology asks.
- **Merge commits** have no diff stats in `git log --numstat` (one row per
  commit), and they are excluded from edit-velocity ranking too.
- Detection is a **lower bound**: agents that leave no trace are
  invisible, so `unlabeled` is never treated as human (PU framing).
- The classifier is still a baseline: no per-brand holdout,
  manual-label comparison or SHAP wiring yet.

## Changes from the first version

Bugs fixed:

- **Windows crashes on non-ASCII text.** Output files and `git log` were
  read and written in the console code page, so the 🤖 in "Generated with
  Claude Code" crashed the run. Everything is now UTF-8.
- **Free-text false positives**: brand words in messages, titles and
  bodies, `openai.com` addresses, logins containing brand names (see
  Detection).
- **`compute_edit_velocity` crashed** (`TypeError`) when any commit lacked
  a date: a naive `datetime.min` was compared with git's offset-aware
  dates.
- **LOC was bytes.** The 5,000 "lines" threshold was compared with the
  languages API's byte count.
- **Commit count came from the window.** With `--since` the clone is
  shallow, so `len(commits)` failed the `>= 100` rule for most repos.
- **Message features leaked the label.** A trailer adds length and lines
  to the message, so the classifier could relearn the heuristic. Signals
  are now stripped first (`features.strip_label_signals`).
- **Rubber stamp** counted any first review, including bots, rather than
  a fast *approval*.
- **The API client** never retried 5xx errors, ignored secondary rate
  limits and 429s, and raised on connection resets.
- **git could hang** waiting for credentials on a deleted or private repo.
  Prompts are now disabled.
- Hour/day of week now use the author date: the committer date of a
  squash or rebase merge is GitHub's merge time.

Added: the 29-brand roster with file markers, agent-written message
markers and AI reviewers; GraphQL repo metadata and PR pages; merge-method
inference and PR-to-commit label propagation; `--until` and `--as-of`;
the `batch` and `report` commands with corpus-level CSVs; public rulesets
for "review required"; and 86 new tests.

## Testing

```bash
pip install -r requirements.txt
pytest -q
```

117 tests. They cover:

- the signature roster, with real true and false positives;
- label priority and PR propagation;
- commit collection, windowing and Unicode against local git repos with
  known ground truth;
- LOC and file markers;
- edit velocity, including its exclusions;
- feature stripping and leakage;
- the API client's retry and rate-limit handling and GraphQL parsing
  (mocked);
- merge-method inference;
- an end-to-end git-only run;
- batch resume and failure isolation.

Live checks run against the real API:

- `smoke-test-api` on `readthedocs/sphinx_rtd_theme` found its Codex
  (`codex/...` branch) and Copilot (`copilot-swe-agent`) PRs.
- A 12-repo pilot took about 2 minutes and 70 requests. The Vim plugins
  and the `osman-haider` case that free-text matching had flagged now come
  out clean.
