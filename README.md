# Agent Fingerprint Pipeline (AFP)

A repo-to-features pipeline for the thesis *"Identifying Coding-Agent
Fingerprints in Commit Metadata"*. It implements the parameters agreed in
the group's shared corpus methodology, from repo eligibility to a
classifier-ready feature table, and runs on a single repo or a whole
corpus (for example a frozen [gitcorpus](https://github.com/Thabettt/gitcorpus)
export) in parallel, resumably.

1. **Repo eligibility + metadata**: stars, fork/archived, push recency,
   lines of code, full commit count, contributor count, and "review
   required" on the default branch.
2. **Commit collection** from a depth-limited `git clone` (the approach
   Robbes et al. use) or, in `--mode api`, from GitHub's GraphQL history
   with no clone at all.
3. **PR and issue collection** via GraphQL: branch, labels, body, author,
   merge method, reviews, AI reviewers, agent assignees, `@agent` mentions
   and agent bot comments.
4. **Detection of 70+ agent brands through every known channel** (the
   full list is in [docs/agent-adoption-guide.pdf](docs/agent-adoption-guide.pdf)):
   commit trailers and tool trailers, agent-written footers, author
   identities and bot accounts, PR branches/labels/bodies, issue
   assignments and mentions, rules/guidance files, CI workflows that run
   agents, MCP config, agent dependencies and dev containers, agent
   sub-folders, `.gitignore` entries and settings that hide agent use.
   Commit labels follow the group's source priority, and the output is
   always `agent_labeled` or `unlabeled`, **never** `human`.
5. **Feature extraction**: message, timing, diff, file-type mix, merge
   type and edit velocity, with every label-source signal stripped
   *before* the features are computed.
6. **Adoption roll-ups** per repo (brands by channel, adoption date,
   hidden use) and per corpus.
7. **Classifier baseline**: logistic regression / gradient boosting,
   repo-level train/test split, PU-learning-aware.

## Install

```bash
git clone https://github.com/Daniel-Nagyy/agent-fingerprint-pipeline
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
repo signals, LOC and features; no PRs, issues, stars, contributors or
eligibility verdict).

**Several tokens:** set `GITHUB_TOKENS=tok1,tok2,tok3` instead. Every
request goes out on the token with the most budget left, and a
rate-limited token is parked while the others keep working, so API
throughput grows with the number of tokens (for example one per group
member).

## Quick start

```bash
# One repo, commits from the data-collection start date onward
python -m afp.cli run spf13/viper --out ./out/viper --since 2026-02-22

# A whole corpus: a gitcorpus corpus.csv (any CSV with a full_name column)
# or a text file with one owner/repo per line
python -m afp.cli batch corpus.csv --out ./out/corpus --since 2026-02-22 --workers 12

# The fast way for many repos: no cloning, everything from the API
python -m afp.cli batch corpus.csv --out ./out/corpus-api --since 2026-02-22 --mode api --workers 16

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
| `--mode clone\|api` | `clone` (default): every feature, exact LOC and file dates. `api`: no clone, about 4x faster; LOC estimated, no file-type-mix features. |
| `--max-prs N` | Newest PRs per repo (default 300; `0` = every PR in the window). |
| `--no-prs` | Skip PR collection. |
| `--max-issues N` | Newest issues scanned per repo (default 200; `0` = all). |
| `--no-issues` | Skip issue collection. |
| `--max-commits N` | API mode: newest commits per repo (default 2000; `0` = all). |

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
- **Two speeds.** On the 185-repo gitcorpus corpus (one token), `--mode api
  --workers 16` took **3.6 minutes** (about 3,000 repos/hour, ~10 requests
  per repo); `--mode clone --workers 12` took 12.2 minutes. Both found the
  same 1,378 commits and the same agents. Use API mode to detect adoption
  across many repos, and clone mode for the repos you train on.
- **Parallel inside each repo.** In clone mode the API calls (metadata,
  PRs, issues) run while the clone downloads.
- **More tokens, more throughput.** With `GITHUB_TOKENS` the API budget is
  the sum of all tokens (5,000 requests and 5,000 GraphQL points per hour
  each).
- **Heavy repos degrade gracefully.** When GitHub's GraphQL times out on a
  page (HTTP 502/504, e.g. 50 issues with long threads), the page size is
  halved and retried instead of failing the repo.
- **No working tree.** Clones use `--no-checkout`: LOC and file markers
  are read from git's object store. Large repos used to spend most of
  their time writing (and, on Windows, virus-scanning) files only to
  count lines; this made the same corpus run 3x faster.
- **Flaky networks.** A clone that fails on the network is retried four
  times and then fails the repo, so `--retry-failed` redoes it. It is
  never mistaken for an empty window: only git's own "no commits after
  that date" answer leads to the depth-1 clone.

## Output

Per repo (`run --out DIR`, or `batch`'s `repos/<owner>__<repo>/`):

| File | Contents |
|---|---|
| `commits.jsonl` | One `CommitRecord` per commit in the window: raw data + full label evidence. |
| `prs.jsonl` | One `PullRequestRecord` per PR (empty without a token or with `--no-prs`). |
| `issues.jsonl` | One `IssueRecord` per issue with agent involvement: agent assignees, `@mentions`, agent bot comments. |
| `features.jsonl` | One `CommitFeatureVector` per commit: what feeds the classifier. |
| `file_markers.jsonl` | Agent rules/guidance/config files present at HEAD, with the commit that first added each (clone mode). |
| `signals.jsonl` | Other repo-level traces: CI workflows, MCP config, dependencies, dev containers, `.gitignore` entries, disabled attribution, agent sub-folders. |
| `summary.json` | Status, mode, window, notes, repo metadata, eligibility, adoption roll-up. |

Corpus level (`batch --out DIR`, rebuilt by `report`):

| File | Contents |
|---|---|
| `repos.csv` | One row per repo: metadata, eligibility (verdict, failed rules, unchecked rules), brands per channel, agent commit/PR shares, adoption date and its source, hidden use, AI reviewers. |
| `adoption_by_brand.csv` | Per brand: kind, repos, repos found through each channel (commit, PR, file, workflow, MCP, agent folder, issue, hidden), agent commits and PRs, for all repos and eligible repos. |
| `file_markers.csv` | Every agent config file found, with its first-added date. |
| `signals.csv` | Every repo signal, with its strength (strong / medium / weak). |
| `commits.jsonl`, `prs.jsonl`, `issues.jsonl`, `features.jsonl` | All repos concatenated (`features.jsonl` is the training set). |
| `corpus_summary.json` | Headline numbers. |

CSV files are UTF-8 with a BOM so Excel opens them correctly.

## Detection

Signatures live in `src/afp/signatures.py`, repo-level checks in
`src/afp/repo_scan.py`. The roster follows Robbes et al.'s validated
heuristic set (file markers, commit trailers, branch names, PR labels),
extended with every trace in [docs/agent-adoption-guide.pdf](docs/agent-adoption-guide.pdf),
each confirmed against live GitHub searches in October 2026. Every brand
has a **kind**: `primary` (the methodology's five: Claude Code, Copilot,
Codex, Gemini, Cursor), `cloud` (Jules, Devin, Antigravity, Kiro, ...),
`ide` (Aider, Amp, OpenCode, Warp, ...), `app_builder` (Lovable, Replit,
v0, ...), `review` (CodeRabbit, Qodo, ...), `tooling` (Spec Kit,
Taskmaster) and `generic` (`unknown`: `AGENTS.md`, an `ai-generated`
label).

| Channel | Examples | Strength |
|---|---|---|
| Commit trailer | `Co-Authored-By: Claude <noreply@anthropic.com>`, `Agent-Logs-Url:`, `Claude-Session:`, `Amp-Thread-ID:`, `Replit-Commit-Author: Agent`, `X-Lovable-Edit-ID:`, `Made-with: Cursor`, `Assisted-by: AGENT:MODEL` | strong |
| Commit author | `Cursor Agent <cursoragent@cursor.com>`, `gpt-engineer-app[bot]`, `Jane Doe (aider)` | strong |
| Message footer | `Generated with [Claude Code]`, `[Gemini CLI]`, `[OpenCode]`, `[Antigravity CLI]`, task/session links | strong |
| PR | bot author, branch prefix (`codex/`, `cline/`, `Q-DEV-issue-`, `jules-<n>-<hash>` ...), label, body footer | strong |
| CI workflow | `anthropics/claude-code-action`, `openai/codex-action`, `run-gemini-cli`, `copilot-setup-steps.yml` | strong |
| Hidden attribution | `includeCoAuthoredBy: false`, aider `attribute-author: false`, "never add Co-authored-by" in guidance | strong |
| Issue | agent assignee (`assignedActors`), agent bot comment | medium |
| Files | `CLAUDE.md`, `.cursor/`, `.github/copilot-instructions.md`, `.kiro/`, ... | medium |
| MCP / agent folders | `.mcp.json`, `.cursor/mcp.json`; `.claude/agents/`, `.github/prompts/` | medium |
| Dev container | Claude Code feature, Cline/Roo/Continue extensions | medium / weak |
| `.gitignore` | `CLAUDE.md`, `.aider*` (aider adds it itself) | medium; weak if template-like |
| Dependency | `@anthropic-ai/claude-code`, `aider-chat` | weak |
| `@mention` | `@claude`, `@codex`, `@copilot` in issues and PRs | weak unless the agent also acted |

A brand counts as **adopted** by a repo when any channel shows it, except
the weak ones (a dependency, a template-like `.gitignore`, a mention the
agent never answered), which are still reported in their own columns.
**AI review is kept apart**: a review bot reviewing or commenting on a PR
goes to `ai_reviewers`, not to adoption. Only a commit it co-authored
(an applied AI fix) labels a commit.

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

- **Depth clones, not `--shallow-since`.** `git clone --shallow-since` cuts
  the history at any commit that has a parent older than the date (a merge
  of an old branch), silently dropping every in-window commit behind it.
  On the 185-repo corpus it lost 219 of 1,378 commits (16%) across 46 repos.
  With a token, clone mode asks the API how many commits lie between the
  start date and HEAD and clones `--depth` that many plus a margin;
  commits are then filtered by date in Python (not `git log --since`,
  which also stops early at out-of-order dates). Without a token it falls
  back to `--shallow-since` and says so in the notes.
- **API mode** gets commits (with GitHub logins, additions, deletions and
  files changed) from GraphQL history, so it sees exactly the same
  commits as clone mode. It has no changed paths (the file-type-mix
  features stay 0) and no file first-added dates. LOC is **estimated** as
  code bytes / 32 (calibrated against the exact counts: the 5,000-line
  rule agrees on 97% of repos); eligibility then reports the LOC rule as
  unchecked and `eligible` is empty instead of true.
- **Eligibility** is true, false, or empty when every rule that could be
  checked passed but one could not (`eligibility_unchecked` says which).

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
- **PR evidence includes outsiders.** An agent PR can be a drive-by
  contribution that is never merged; `agent_prs_merged` counts the ones
  the maintainers accepted.
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
for "review required"; no-checkout clones; and 88 new tests.

## Testing

```bash
pip install -r requirements.txt
pytest -q
```

176 tests. They cover:

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
- batch resume and failure isolation;
- every trace family in the adoption guide (real true and false
  positives), repo signals, issues and mentions, review-bot separation;
- API mode end to end, depth sizing, the token pool, page-size halving.

Live checks run against the real API:

- `smoke-test-api` on `readthedocs/sphinx_rtd_theme` found its Codex
  (`codex/...` branch) and Copilot (`copilot-swe-agent`) PRs.
- A 12-repo pilot took about 2 minutes and 70 requests. The Vim plugins
  and the `osman-haider` case that free-text matching had flagged now come
  out clean.
- The full 185-repo gitcorpus corpus (window 2025-08-12 to 2026-02-21,
  `--as-of 2026-02-21`, 8 workers) took 10.1 minutes and 944 requests,
  with 0 failures. 125 repos met every eligibility rule and 20 showed
  agent traces. Every Claude Code PR hit that the earlier detection run
  had not reported was checked by hand: each is the agent's own
  "Generated with [Claude Code]" footer or a `claude/` branch. Window
  commit counts for the repos that had disagreed between test runs now
  match `git rev-list` exactly.
- After the full extension (Oct 2026): both modes on the same corpus found
  1,378 commits, 1,126 PRs and 21 repos with agent use (Claude Code 11,
  Copilot 6, Codex 3, Cursor, Junie, Jules, Aider 1 each; 18 with
  agent-written commits or PRs; 1 with hidden use). Clone mode before the
  depth fix had found only 1,159 commits. Every newly found brand was
  checked by hand; review-only bots (Gemini Code Assist, CodeRabbit on
  dependabot PRs) were first counted as adoption, which led to keeping AI
  review apart.
