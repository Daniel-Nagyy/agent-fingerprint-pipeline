"""
Constants agreed in Shared_Corpus_Methodology.tex.

Keeping these in one module means a group decision change (e.g. adding
Cursor back in, changing the LOC threshold) is a one-line edit instead
of a hunt through the codebase.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Repo eligibility (Table: "Repo eligibility and time window parameters")
# ---------------------------------------------------------------------------

MIN_LOC = 5_000
MIN_COMMITS = 100
MIN_STARS = 10
ALLOW_FORKS = False
PUSH_RECENCY_MONTHS = 12          # "active/pushed in last 12 months"
DATA_COLLECTION_START = "2026-02-22"  # Robbes et al. cover up to 2026-02-21

SIZE_TIER_BOUNDARIES = {
    # (min_loc_inclusive, max_loc_exclusive) -> tier label
    "small": (5_000, 20_000),
    "medium": (20_000, 100_000),
    "large": (100_000, None),
}

# ---------------------------------------------------------------------------
# Agent identification (Table: "Agent identification parameters")
# ---------------------------------------------------------------------------

# Top five brands per Robbes et al. (2026) adoption figures.
AGENT_BRANDS = ["claude_code", "copilot", "codex", "gemini", "cursor"]

# Co-author trailer patterns -> brand. Matched case-insensitively against
# "Co-authored-by: <name> <<email>>" trailer lines in the commit message.
TRAILER_BRAND_PATTERNS = {
    "claude_code": [r"claude", r"claude code"],
    "copilot": [r"copilot"],
    "codex": [r"codex", r"openai"],
    "gemini": [r"gemini", r"antigravity"],
    "cursor": [r"cursor"],
}

# Known bot account logins (GitHub `author.type == "Bot"` plus well-known
# app-bot usernames). Extend as new tool bots are identified.
BOT_ACCOUNT_LOGINS = {
    "github-actions[bot]": None,          # not an agent brand, CI bot -- excluded from agent labeling
    "copilot-swe-agent[bot]": "copilot",
    "copilot[bot]": "copilot",
    "claude[bot]": "claude_code",
    "cursor[bot]": "cursor",
    "codex[bot]": "codex",
    "gemini-code-assist[bot]": "gemini",
}
# Logins mapped to None are recognised bots but NOT counted as agent-authored
# (e.g. plain CI bots unrelated to coding agents).

# Known committer/author e-mail domains associated with an agent brand.
EMAIL_DOMAIN_BRANDS = {
    "noreply@anthropic.com": "claude_code",
    "noreply.anthropic.com": "claude_code",
    "openai.com": "codex",
    "github.com": None,  # too generic (covers normal noreply GitHub emails) -- not used alone
}

# Substrings searched for in PR title/body/branch-name/labels.
PR_MARKER_PATTERNS = {
    "claude_code": [r"claude code", r"claude-code", r"\bclaude\b"],
    "copilot": [r"copilot"],
    "codex": [r"\bcodex\b"],
    "gemini": [r"gemini", r"antigravity"],
    "cursor": [r"\bcursor\b"],
}

# Label-source priority, checked in this order (per methodology Notes item 2).
LABEL_SOURCE_PRIORITY = ["trailer", "bot_account", "email_domain", "pr_marker"]

LABEL_AGENT = "agent_labeled"
LABEL_UNLABELED = "unlabeled"   # never "human" -- see Positive/Unlabeled framing

# ---------------------------------------------------------------------------
# Commit-level features (Table: "Commit-level feature parameters")
# ---------------------------------------------------------------------------

CONVENTIONAL_COMMIT_PREFIXES = (
    "feat", "fix", "docs", "style", "refactor", "perf", "test", "build",
    "ci", "chore", "revert",
)

FILE_TYPE_GROUPS = {
    "source": {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h",
        ".hpp", ".go", ".rs", ".rb", ".php", ".cs", ".swift", ".kt", ".scala",
    },
    "docs": {".md", ".rst", ".txt", ".adoc"},
    "config": {
        ".yml", ".yaml", ".json", ".toml", ".ini", ".cfg", ".env",
        "dockerfile", ".lock",
    },
    "test": set(),  # determined by path heuristics (see features.py), not extension
}

EDIT_VELOCITY_FLAG_PERCENTILE = 95  # top 5% flagged as high-velocity

# ---------------------------------------------------------------------------
# PR-level features (Table: "Pull request-level feature parameters")
# ---------------------------------------------------------------------------

RUBBER_STAMP_MAX_COMMENTS = 0
RUBBER_STAMP_MAX_LATENCY_SECONDS = 15 * 60  # "fast approval" -- 15 minutes; group to confirm

# ---------------------------------------------------------------------------
# Stack / language tagging
# ---------------------------------------------------------------------------

LANGUAGE_TO_STACK_TAG = {
    "Python": "Python",
    "JavaScript": "JS/TS",
    "TypeScript": "JS/TS",
    "Java": "Java",
    "PHP": "PHP",
    "Rust": "Rust",
    "C#": "C#",
}
