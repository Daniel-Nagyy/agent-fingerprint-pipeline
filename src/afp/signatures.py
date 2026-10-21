"""
Agent signatures: every explicit trace a coding agent is known to leave.

The roster and channel split follow Robbes et al. (2026)'s validated
heuristic set (file markers, commit trailers, branch names, PR labels),
extended with the bot accounts, e-mail addresses and body markers the
agents themselves write. Every pattern is *anchored* -- a brand name
appearing anywhere in free text is never a signal. That free-text
matching ("move the cursor", a co-author called Claude) was the main
false-positive source when this roster was first run on a real corpus.

Brands are snake_case ids. PRIMARY_BRANDS are the five the methodology
analyses; everything else is still detected and reported, so a repo
adopting e.g. Devin or Aider is not silently counted as "no agent".
"unknown" is the methodology's catch-all for generic agent traces
(AGENTS.md, an "ai-generated" label) that do not name a brand.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Pattern, Tuple

PRIMARY_BRANDS = ("claude_code", "copilot", "codex", "gemini", "cursor")

BRAND_NAMES: Dict[str, str] = {
    "claude_code": "Claude Code",
    "copilot": "Copilot",
    "codex": "Codex",
    "gemini": "Gemini",
    "cursor": "Cursor",
    "aider": "Aider",
    "amazon_q": "Amazon Q",
    "amp": "Amp",
    "cline": "Cline",
    "continue": "Continue",
    "crush": "Crush",
    "devin": "Devin",
    "factory": "Factory",
    "goose": "Goose",
    "jules": "Jules",
    "junie": "Junie",
    "kilo_code": "Kilo Code",
    "kiro": "Kiro",
    "letta": "Letta Code",
    "mistral_vibe": "Mistral Vibe",
    "ona": "Ona",
    "opencode": "Opencode",
    "openhands": "OpenHands",
    "qwen_coder": "Qwen Coder",
    "roo_code": "Roo Code",
    "sweep": "Sweep",
    "trae": "Trae",
    "warp": "Warp",
    "windsurf": "Windsurf",
    "unknown": "Unknown agent",
}

# ---------------------------------------------------------------------------
# Identities: exact e-mail addresses and GitHub logins
# ---------------------------------------------------------------------------

# Exact, lower-cased addresses. Whole domains are deliberately NOT mapped:
# openai.com / anthropic.com staff commit to open source as humans.
AGENT_EMAILS: Dict[str, str] = {
    "noreply@anthropic.com": "claude_code",
    "claude@anthropic.com": "claude_code",
    "cursoragent@cursor.com": "cursor",
    "codex@openai.com": "codex",
    "noreply@openai.com": "codex",
    "aider@aider.chat": "aider",
    "openhands@all-hands.dev": "openhands",
    "cascade@windsurf.ai": "windsurf",
    "noreply@opencode.ai": "opencode",
    "amp@ampcode.com": "amp",
    "kiro@aws.amazon.com": "kiro",
    "kiro-agent@amazon.com": "kiro",
    "kiro-noreply@amazon.com": "kiro",
    "noreply@warp.dev": "warp",
    "roomote@roocode.com": "roo_code",
    "crush@charm.land": "crush",
    "qwen-coder@alibabacloud.com": "qwen_coder",
    "vibe@mistral.ai": "mistral_vibe",
    "noreply@letta.com": "letta",
    "no-reply@ona.com": "ona",
}

# Lower-cased GitHub logins of accounts that *author* code for an agent.
# Plain names ("claude", "cursor", "jules") are only trusted with the
# [bot] suffix, since humans hold those logins; "copilot" is GitHub's own
# reserved account.
AGENT_LOGINS: Dict[str, str] = {
    "copilot": "copilot",
    "copilot[bot]": "copilot",
    "copilot-swe-agent": "copilot",
    "copilot-swe-agent[bot]": "copilot",
    "claude[bot]": "claude_code",
    "chatgpt-codex-connector[bot]": "codex",
    "codex[bot]": "codex",
    "cursor[bot]": "cursor",
    "gemini-code-assist[bot]": "gemini",
    "gemini-cli[bot]": "gemini",
    "google-labs-jules[bot]": "jules",
    "devin-ai-integration[bot]": "devin",
    "openhands-agent": "openhands",
    "sweep-ai[bot]": "sweep",
    "jetbrains-junie[bot]": "junie",
    "amazon-q-developer[bot]": "amazon_q",
    "factory-droid[bot]": "factory",
    "windsurf-bot[bot]": "windsurf",
    "roomote-agent": "roo_code",
}

# AI tools that review but do not author. Recorded on PRs as reviewers,
# never as an authorship label.
REVIEW_BOT_LOGINS: Dict[str, str] = {
    "coderabbitai[bot]": "CodeRabbit",
    "coderabbitai": "CodeRabbit",
    "sourcery-ai[bot]": "Sourcery",
    "gemini-code-assist[bot]": "Gemini Code Assist",
    "copilot-pull-request-reviewer[bot]": "Copilot review",
    "cursor[bot]": "Cursor Bugbot",
    "qodo-merge-pro[bot]": "Qodo",
    "greptile-apps[bot]": "Greptile",
}

# Automation that is not a coding agent (dependency bumps, CI).
CI_BOT_LOGINS = {
    "github-actions[bot]", "dependabot[bot]", "renovate[bot]", "snyk-bot",
    "pre-commit-ci[bot]", "codecov[bot]", "allcontributors[bot]",
    "imgbot[bot]", "mergify[bot]", "stale[bot]",
}

_NOREPLY_RE = re.compile(r"^(?:\d+\+)?(?P<login>[^@]+)@users\.noreply\.github\.com$", re.I)


def login_from_noreply(email: Optional[str]) -> Optional[str]:
    """`123+copilot-swe-agent[bot]@users.noreply.github.com` -> the login."""
    if not email:
        return None
    m = _NOREPLY_RE.match(email.strip())
    return m.group("login").lower() if m else None


def brand_for_login(login: Optional[str]) -> Optional[str]:
    if not login:
        return None
    return AGENT_LOGINS.get(login.strip().lower())


def brand_for_email(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    email_l = email.strip().lower()
    if email_l in AGENT_EMAILS:
        return AGENT_EMAILS[email_l]
    return brand_for_login(login_from_noreply(email_l))


def is_ci_bot(login: Optional[str]) -> bool:
    return bool(login) and login.strip().lower() in CI_BOT_LOGINS


def _compile(table: Dict[str, List[str]], flags: int = re.I) -> List[Tuple[str, Pattern[str]]]:
    return [(brand, re.compile(p, flags)) for brand, pats in table.items() for p in pats]


# ---------------------------------------------------------------------------
# Trailer co-author names (used only when the trailer's e-mail is unknown)
# ---------------------------------------------------------------------------

TRAILER_NAME_PATTERNS = _compile({
    "claude_code": [r"^claude(?: code)?$", r"^claude (?:opus|sonnet|haiku)\b", r"^claude \("],
    "copilot": [r"^(?:github )?copilot$"],
    "cursor": [r"^cursor(?: agent)?$"],
    "codex": [r"^codex\b", r"^chatgpt codex\b"],
    "gemini": [r"^gemini(?: code assist| cli)?$", r"^gemini \d"],
    "aider": [r"^aider$", r"^aider \("],
    "devin": [r"^devin ai$"],
    "junie": [r"^junie$"],
    "openhands": [r"^openhands(?:-agent)?$"],
    "qwen_coder": [r"^qwen[- ]coder$"],
})

TRAILER_RE = re.compile(
    r"^\s*(?P<key>co-authored-by|generated-by|assisted-by)\s*:\s*"
    r"(?P<name>[^<\n]*?)\s*(?:<(?P<email>[^>\n]*)>)?\s*$",
    re.I | re.M,
)

# ---------------------------------------------------------------------------
# Markers the agents write into commit messages and PR bodies
# ---------------------------------------------------------------------------

BODY_MARKER_PATTERNS = _compile({
    "claude_code": [r"generated with \[?claude code\]?", r"https?://claude\.(?:ai/code|com/claude-code)"],
    "codex": [r"chatgpt\.com/codex/tasks/"],
    "copilot": [r"copilot-workspace\.githubnext\.com"],
    "devin": [r"app\.devin\.ai/sessions/"],
    "cursor": [r"cursor\.com/background-agent"],
    "amp": [r"ampcode\.com/threads/"],
    "opencode": [r"generated with \[?opencode\]?"],
})

# A commit subject the tool itself prefixes (classic Aider behaviour).
SUBJECT_PREFIX_PATTERNS = _compile({
    "aider": [r"^aider: "],
})

# ---------------------------------------------------------------------------
# PR head-branch prefixes and exact PR labels
# ---------------------------------------------------------------------------

BRANCH_PREFIXES: Dict[str, str] = {
    "claude/": "claude_code",
    "claudecode/": "claude_code",
    "copilot/": "copilot",
    "codex/": "codex",
    "cursor/": "cursor",
    "devin/": "devin",
    "jules/": "jules",
    "openhands/": "openhands",
    "opencode/": "opencode",
    "sweep/": "sweep",
    "goose/": "goose",
    "open-swe/": "unknown",
    "codegen-bot/": "unknown",
    "windsurf-bot/": "windsurf",
}

PR_LABELS: Dict[str, str] = {
    "codex": "codex",
    "ai-generated": "unknown",
    "copilot-generated": "copilot",
    "claude-code": "claude_code",
}


def brand_for_branch(branch: Optional[str]) -> Optional[str]:
    if not branch:
        return None
    b = branch.strip().lower()
    for prefix, brand in BRANCH_PREFIXES.items():
        if b.startswith(prefix):
            return brand
    if b.startswith("jetbrains_junie"):
        return "junie"
    return None


def brand_for_label(label: Optional[str]) -> Optional[str]:
    if not label:
        return None
    return PR_LABELS.get(label.strip().lower())


# ---------------------------------------------------------------------------
# Repo file markers (configuration an agent reads or writes)
# ---------------------------------------------------------------------------

# (path or directory prefix ending in "/", brand). Matched against
# repo-relative paths, case-sensitive, from the repo root unless the entry
# starts with "**/" (any depth).
FILE_MARKERS: List[Tuple[str, str]] = [
    ("CLAUDE.md", "claude_code"),
    ("**/CLAUDE.md", "claude_code"),
    (".claude/", "claude_code"),
    (".claudeignore", "claude_code"),
    (".github/workflows/claude.yml", "claude_code"),
    (".github/workflows/claude-code-review.yml", "claude_code"),
    (".github/copilot-instructions.md", "copilot"),
    (".github/instructions/", "copilot"),
    (".github/prompts/", "copilot"),
    (".github/workflows/copilot-setup-steps.yml", "copilot"),
    (".copilotignore", "copilot"),
    (".cursorrules", "cursor"),
    (".cursor/", "cursor"),
    ("CURSOR.md", "cursor"),
    (".codex/", "codex"),
    ("GEMINI.md", "gemini"),
    (".gemini/", "gemini"),
    ("AGENTS.md", "unknown"),
    ("**/AGENTS.md", "unknown"),
    (".aider.conf.yml", "aider"),
    (".aider.conf.yaml", "aider"),
    (".aiderignore", "aider"),
    (".clinerules", "cline"),
    (".cline/", "cline"),
    (".roo/", "roo_code"),
    (".roorules", "roo_code"),
    (".windsurfrules", "windsurf"),
    (".windsurf/", "windsurf"),
    (".junie/", "junie"),
    (".jules/", "jules"),
    (".devin/", "devin"),
    (".openhands/", "openhands"),
    (".goosehints", "goose"),
    (".continue/", "continue"),
    (".kiro/", "kiro"),
    (".kilocode/", "kilo_code"),
    (".amazonq/", "amazon_q"),
    ("AmazonQ.md", "amazon_q"),
    (".trae/", "trae"),
    ("WARP.md", "warp"),
    ("CRUSH.md", "crush"),
    ("QWEN.md", "qwen_coder"),
    (".opencode/", "opencode"),
    ("opencode.json", "opencode"),
    ("AGENT.md", "amp"),
    (".factory/", "factory"),
]


def file_marker_brand(path: str) -> Optional[Tuple[str, str]]:
    """Return (marker, brand) if a repo-relative path is an agent marker."""
    p = path.replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    base = p.rsplit("/", 1)[-1]
    for marker, brand in FILE_MARKERS:
        if marker.startswith("**/"):
            if base == marker[3:] and "/" in p:
                return marker, brand
        elif marker.endswith("/"):
            if p.startswith(marker):
                return marker, brand
        elif p == marker:
            return marker, brand
    return None
