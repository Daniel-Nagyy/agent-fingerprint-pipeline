"""
Explicit-signal heuristics: trailer / bot account / e-mail domain / PR marker
detection, with brand resolution and label-source priority.

This module ONLY looks for unambiguous, explicit signals. It never guesses
from structural/statistical properties -- that is features.py's job, and
keeping the two separate is what makes the "strip signals before training"
step in schema.CommitFeatureVector meaningful rather than circular.

Output label is always "agent_labeled" or "unlabeled" -- never "human".
Absence of a signal means "we don't know", not "this is human-written".
"""

from __future__ import annotations

import re
from typing import Optional, Iterable, List

from . import config
from .schema import LabelSignal


_TRAILER_RE = re.compile(
    r"^\s*co-authored-by\s*:\s*(?P<name>[^<]*)<(?P<email>[^>]+)>\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _match_brand_patterns(text: str, pattern_map: dict) -> Optional[str]:
    """Return the first brand whose pattern list matches `text`, else None."""
    if not text:
        return None
    lowered = text.lower()
    for brand, patterns in pattern_map.items():
        for pat in patterns:
            if re.search(pat, lowered, re.IGNORECASE):
                return brand
    return None


def detect_trailer(commit_message: str) -> LabelSignal:
    """Look for a 'Co-authored-by: ... <...>' trailer naming a known agent."""
    sig = LabelSignal()
    if not commit_message:
        return sig
    for m in _TRAILER_RE.finditer(commit_message):
        name = (m.group("name") or "").strip()
        email = (m.group("email") or "").strip()
        combined = f"{name} {email}"
        brand = _match_brand_patterns(combined, config.TRAILER_BRAND_PATTERNS)
        if brand:
            sig.has_trailer = True
            sig.trailer_text = m.group(0).strip()
            sig.agent_brand = brand
            sig.label_source = "trailer"
            sig.label = config.LABEL_AGENT
            return sig
        # trailer exists but doesn't match a known brand -- keep looking,
        # but record that *a* trailer was present in case nothing else hits
        if not sig.has_trailer:
            sig.has_trailer = True
            sig.trailer_text = m.group(0).strip()
    return sig


def detect_bot_account(author_login: Optional[str]) -> LabelSignal:
    """Look up a GitHub account login against the known bot-account map."""
    sig = LabelSignal()
    if not author_login:
        return sig
    login = author_login.strip()
    if login in config.BOT_ACCOUNT_LOGINS:
        brand = config.BOT_ACCOUNT_LOGINS[login]
        sig.is_bot_account = True
        sig.bot_account_login = login
        if brand:
            sig.agent_brand = brand
            sig.label_source = "bot_account"
            sig.label = config.LABEL_AGENT
    return sig


def detect_email_domain(email: Optional[str]) -> LabelSignal:
    """Look up a committer/author e-mail against known agent e-mail domains."""
    sig = LabelSignal()
    if not email:
        return sig
    email_l = email.strip().lower()
    domain = email_l.split("@")[-1] if "@" in email_l else email_l

    # exact full-address match first (e.g. "noreply@anthropic.com")
    if email_l in config.EMAIL_DOMAIN_BRANDS and config.EMAIL_DOMAIN_BRANDS[email_l]:
        sig.email_domain_match = True
        sig.email_domain = email_l
        sig.agent_brand = config.EMAIL_DOMAIN_BRANDS[email_l]
        sig.label_source = "email_domain"
        sig.label = config.LABEL_AGENT
        return sig

    # then domain-only match
    if domain in config.EMAIL_DOMAIN_BRANDS and config.EMAIL_DOMAIN_BRANDS[domain]:
        sig.email_domain_match = True
        sig.email_domain = domain
        sig.agent_brand = config.EMAIL_DOMAIN_BRANDS[domain]
        sig.label_source = "email_domain"
        sig.label = config.LABEL_AGENT
    return sig


def detect_pr_marker(title: str = "", body: str = "",
                      branch_name: str = "", labels: Optional[Iterable[str]] = None) -> LabelSignal:
    """Look for an agent marker in PR title/body/branch name/labels."""
    sig = LabelSignal()
    labels = list(labels or [])
    combined = " ".join([title or "", body or "", branch_name or "", " ".join(labels)])
    brand = _match_brand_patterns(combined, config.PR_MARKER_PATTERNS)
    if brand:
        sig.pr_marker_match = True
        sig.pr_marker_text = combined[:200]
        sig.agent_brand = brand
        sig.label_source = "pr_marker"
        sig.label = config.LABEL_AGENT
    return sig


def resolve_label(
    *,
    commit_message: str = "",
    author_login: Optional[str] = None,
    author_email: Optional[str] = None,
    committer_email: Optional[str] = None,
    pr_title: str = "",
    pr_body: str = "",
    pr_branch_name: str = "",
    pr_labels: Optional[Iterable[str]] = None,
) -> LabelSignal:
    """Combine every available signal and resolve a single LabelSignal,
    respecting config.LABEL_SOURCE_PRIORITY (trailer > bot_account >
    email_domain > pr_marker) for which brand/source gets reported,
    while keeping every raw sub-signal that fired on the result.
    """
    trailer_sig = detect_trailer(commit_message)
    bot_sig = detect_bot_account(author_login)
    email_sig = detect_email_domain(author_email) if author_email else LabelSignal()
    if not email_sig.email_domain_match and committer_email:
        committer_sig = detect_email_domain(committer_email)
        if committer_sig.email_domain_match:
            email_sig = committer_sig
    pr_sig = detect_pr_marker(pr_title, pr_body, pr_branch_name, pr_labels)

    by_source = {
        "trailer": trailer_sig,
        "bot_account": bot_sig,
        "email_domain": email_sig,
        "pr_marker": pr_sig,
    }

    result = LabelSignal(
        has_trailer=trailer_sig.has_trailer,
        trailer_text=trailer_sig.trailer_text,
        is_bot_account=bot_sig.is_bot_account,
        bot_account_login=bot_sig.bot_account_login,
        email_domain_match=email_sig.email_domain_match,
        email_domain=email_sig.email_domain,
        pr_marker_match=pr_sig.pr_marker_match,
        pr_marker_text=pr_sig.pr_marker_text,
    )

    for source in config.LABEL_SOURCE_PRIORITY:
        sig = by_source[source]
        if sig.label == config.LABEL_AGENT:
            result.agent_brand = sig.agent_brand
            result.label_source = source
            result.label = config.LABEL_AGENT
            return result

    result.label = config.LABEL_UNLABELED
    result.agent_brand = None
    result.label_source = None
    return result
