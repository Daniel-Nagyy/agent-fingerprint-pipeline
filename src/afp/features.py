"""
Structural/statistical feature extraction, per "Commit-level feature
parameters". Turns a CommitRecord into a CommitFeatureVector with every
explicit label-source signal stripped out -- this is the boundary that
keeps the PU-learning setup honest (schema.py's docstring on
CommitFeatureVector explains why).

compute_commit_features() assumes compute_edit_velocity() (collect.py)
has already annotated the commits it's given, since edit velocity needs
the whole per-author commit sequence and can't be computed one commit
at a time.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional

from . import config
from .collect import _classify_file  # reuse the same file-type classifier
from .schema import CommitRecord, CommitFeatureVector

_CONVENTIONAL_PREFIX_RE = re.compile(
    r"^(" + "|".join(config.CONVENTIONAL_COMMIT_PREFIXES) + r")(\(.+?\))?!?:\s",
    re.IGNORECASE,
)

_TEMPLATE_MARKERS = [
    r"^merge branch",
    r"^merge pull request",
    r"^signed-off-by:",
    r"^this is an automated",
    r"^auto-generated",
    r"^bump .+ from .+ to .+",   # dependabot-style
]
_TEMPLATE_MARKER_RES = [re.compile(p, re.IGNORECASE) for p in _TEMPLATE_MARKERS]


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _message_features(message: str):
    length = len(message)
    line_count = max(1, message.count("\n") + 1)
    first_line = message.splitlines()[0] if message else ""
    has_prefix = bool(_CONVENTIONAL_PREFIX_RE.match(first_line))

    marker_hits = sum(1 for r in _TEMPLATE_MARKER_RES if r.search(message))
    # crude 0..1 template-likeness: marker hit is a strong signal; very
    # short, generic first lines ("fix", "update", "wip") nudge it up too.
    generic_short = first_line.strip().lower() in {"fix", "update", "wip", "misc", "cleanup"}
    template_likeness = min(1.0, 0.6 * marker_hits + (0.3 if generic_short else 0.0))

    return length, line_count, has_prefix, template_likeness


def _file_type_mix(file_paths: List[str]):
    if not file_paths:
        return 0.0, 0.0, 0.0, 0.0
    counts = {"source": 0, "docs": 0, "config": 0, "test": 0, "other": 0}
    for p in file_paths:
        counts[_classify_file(p)] += 1
    n = len(file_paths)
    return (
        counts["source"] / n,
        counts["docs"] / n,
        counts["config"] / n,
        counts["test"] / n,
    )


def commit_to_feature_vector(commit: CommitRecord) -> CommitFeatureVector:
    length, line_count, has_prefix, template_likeness = _message_features(commit.message)
    frac_source, frac_docs, frac_config, frac_test = _file_type_mix(commit.file_paths)

    dt = _parse_dt(commit.commit_date or commit.author_date)
    hour_of_day = dt.hour if dt else None
    day_of_week = dt.weekday() if dt else None

    add_delete_ratio = None
    if commit.deletions > 0:
        add_delete_ratio = commit.additions / commit.deletions
    elif commit.additions > 0:
        add_delete_ratio = float("inf")

    pu_label = 1 if commit.label.label == config.LABEL_AGENT else 0

    return CommitFeatureVector(
        repo_full_name=commit.repo_full_name,
        sha=commit.sha,
        message_length=length,
        message_line_count=line_count,
        has_conventional_prefix=has_prefix,
        template_likeness=template_likeness,
        seconds_since_prev_commit=commit.seconds_since_author_prev_commit,
        hour_of_day=hour_of_day,
        day_of_week=day_of_week,
        additions=commit.additions,
        deletions=commit.deletions,
        files_changed=commit.files_changed,
        add_delete_ratio=add_delete_ratio,
        frac_source=frac_source,
        frac_docs=frac_docs,
        frac_config=frac_config,
        frac_test=frac_test,
        is_merge=commit.is_merge,
        merge_type=commit.merge_type,
        edit_velocity=commit.edit_velocity,
        edit_velocity_flagged=commit.edit_velocity_flagged,
        pu_label=pu_label,
    )


def commits_to_feature_vectors(commits: List[CommitRecord]) -> List[CommitFeatureVector]:
    return [commit_to_feature_vector(c) for c in commits]
