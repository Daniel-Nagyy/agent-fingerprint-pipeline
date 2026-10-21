"""
Adoption roll-ups: per repo (which agents left traces, through which
source, what share of the window they account for) and per corpus
(merging every repo's output into combined files and one table per
brand).

Everything here reads records the pipeline already produced; nothing
calls git or the API.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import config
from .schema import CommitRecord, FileMarker, PullRequestRecord, RepoAdoption
from .signatures import BRAND_NAMES, PRIMARY_BRANDS


def summarize_repo(full_name: str, commits: List[CommitRecord], prs: List[PullRequestRecord],
                   markers: List[FileMarker]) -> RepoAdoption:
    agent_commits = [c for c in commits if c.label.label == config.LABEL_AGENT]
    agent_prs = [p for p in prs if p.label.label == config.LABEL_AGENT]
    commit_brands = Counter(c.label.agent_brand for c in agent_commits)
    pr_brands = Counter(p.label.agent_brand for p in agent_prs)
    sources = Counter(c.label.label_source for c in agent_commits)
    sources.update(f"pr:{p.label.label_source}" for p in agent_prs)
    marker_brands = sorted({m.brand for m in markers})

    brands: List[str] = []
    for brand in list(commit_brands) + list(pr_brands) + marker_brands:
        if brand not in brands:
            brands.append(brand)
    for c in agent_commits:  # secondary brands a commit also named
        for b in c.label.brands:
            if b not in brands:
                brands.append(b)

    dates = sorted(c.author_date for c in agent_commits if c.author_date)
    reviewers: List[str] = []
    for p in prs:
        for r in p.ai_reviewers:
            if r not in reviewers:
                reviewers.append(r)

    return RepoAdoption(
        full_name=full_name,
        commits_in_window=len(commits),
        agent_commits=len(agent_commits),
        agent_commit_share=(len(agent_commits) / len(commits)) if commits else None,
        prs_collected=len(prs),
        agent_prs=len(agent_prs),
        agent_pr_share=(len(agent_prs) / len(prs)) if prs else None,
        brands=brands,
        primary_brands=[b for b in brands if b in PRIMARY_BRANDS],
        commit_brand_counts=dict(commit_brands),
        pr_brand_counts=dict(pr_brands),
        file_marker_brands=marker_brands,
        label_source_counts=dict(sources),
        first_agent_commit_at=dates[0] if dates else None,
        ai_reviewers=reviewers,
    )


# ---------------------------------------------------------------------------
# Corpus-level merge
# ---------------------------------------------------------------------------

REPO_COLUMNS = [
    "full_name", "status", "error", "mode", "eligible", "eligibility_reasons",
    "stars", "primary_language", "stack_tag", "total_loc", "size_tier", "commit_count",
    "contributor_count", "is_solo", "branch_protection", "pushed_at",
    "commits_in_window", "agent_commits", "agent_commit_share",
    "prs_collected", "agent_prs", "agent_pr_share",
    "any_agent", "brands", "primary_brands", "file_marker_brands", "first_agent_commit_at",
    "ai_reviewers", "label_sources",
]


def _flat_repo_row(summary: Dict[str, Any]) -> Dict[str, Any]:
    meta = summary.get("repo_metadata") or {}
    elig = summary.get("eligibility") or {}
    ad = summary.get("adoption") or {}
    return {
        "full_name": summary.get("full_name"),
        "status": summary.get("status"),
        "error": summary.get("error"),
        "mode": summary.get("mode"),
        "eligible": elig.get("eligible"),
        "eligibility_reasons": "; ".join(elig.get("reasons") or []),
        "stars": meta.get("stars"),
        "primary_language": meta.get("primary_language"),
        "stack_tag": meta.get("stack_tag"),
        "total_loc": meta.get("total_loc"),
        "size_tier": meta.get("size_tier"),
        "commit_count": meta.get("commit_count"),
        "contributor_count": meta.get("contributor_count"),
        "is_solo": meta.get("is_solo"),
        "branch_protection": meta.get("branch_protection"),
        "pushed_at": meta.get("pushed_at"),
        "commits_in_window": ad.get("commits_in_window"),
        "agent_commits": ad.get("agent_commits"),
        "agent_commit_share": ad.get("agent_commit_share"),
        "prs_collected": ad.get("prs_collected"),
        "agent_prs": ad.get("agent_prs"),
        "agent_pr_share": ad.get("agent_pr_share"),
        "any_agent": bool(ad.get("brands")) if ad else None,
        "brands": ";".join(ad.get("brands") or []),
        "primary_brands": ";".join(ad.get("primary_brands") or []),
        "file_marker_brands": ";".join(ad.get("file_marker_brands") or []),
        "first_agent_commit_at": ad.get("first_agent_commit_at"),
        "ai_reviewers": ";".join(ad.get("ai_reviewers") or []),
        "label_sources": ";".join(f"{k}={v}" for k, v in sorted((ad.get("label_source_counts") or {}).items())),
    }


def _concat_jsonl(sources: Iterable[Path], dest: Path) -> int:
    n = 0
    with open(dest, "w", encoding="utf-8", newline="\n") as out:
        for src in sources:
            if not src.exists():
                continue
            with open(src, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        out.write(line if line.endswith("\n") else line + "\n")
                        n += 1
    return n


def merge_corpus(out_dir: str) -> Dict[str, Any]:
    """Combine every repos/<owner__repo>/ output under `out_dir` into
    corpus-wide files and return the headline numbers:

      repos.csv            one row per repo: metadata, eligibility, adoption
      adoption_by_brand.csv  repos / commits / PRs per brand, all repos and eligible only
      file_markers.csv     every agent config file found, with first-added date
      commits.jsonl, prs.jsonl, features.jsonl   all repos concatenated
    """
    out = Path(out_dir)
    repo_dirs = sorted(p for p in (out / "repos").glob("*") if p.is_dir())
    summaries = []
    for d in repo_dirs:
        s_path = d / "summary.json"
        if s_path.exists():
            with open(s_path, encoding="utf-8") as f:
                summaries.append(json.load(f))
    ok = [s for s in summaries if s.get("status") == "ok"]

    with open(out / "repos.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REPO_COLUMNS)
        w.writeheader()
        for s in summaries:
            w.writerow(_flat_repo_row(s))

    def _brand_table(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
        table: Dict[str, Dict[str, int]] = {}
        for s in rows:
            ad = s.get("adoption") or {}
            for brand in ad.get("brands") or []:
                t = table.setdefault(brand, {"repos": 0, "agent_commits": 0, "agent_prs": 0, "repos_with_file_marker": 0})
                t["repos"] += 1
                t["agent_commits"] += (ad.get("commit_brand_counts") or {}).get(brand, 0)
                t["agent_prs"] += (ad.get("pr_brand_counts") or {}).get(brand, 0)
                if brand in (ad.get("file_marker_brands") or []):
                    t["repos_with_file_marker"] += 1
        return table

    eligible = [s for s in ok if (s.get("eligibility") or {}).get("eligible")]
    all_table, elig_table = _brand_table(ok), _brand_table(eligible)
    with open(out / "adoption_by_brand.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["brand", "brand_name", "primary", "repos", "repo_share", "agent_commits", "agent_prs",
                    "repos_with_file_marker", "eligible_repos", "eligible_repo_share"])
        for brand in sorted(all_table, key=lambda b: (-all_table[b]["repos"], b)):
            t, e = all_table[brand], elig_table.get(brand, {"repos": 0})
            w.writerow([brand, BRAND_NAMES.get(brand, brand), brand in PRIMARY_BRANDS, t["repos"],
                        round(t["repos"] / len(ok), 4) if ok else None, t["agent_commits"], t["agent_prs"],
                        t["repos_with_file_marker"], e["repos"],
                        round(e["repos"] / len(eligible), 4) if eligible else None])

    with open(out / "file_markers.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["full_name", "brand", "marker", "path", "first_added_at", "first_added_sha"])
        for d in repo_dirs:
            p = d / "file_markers.jsonl"
            if not p.exists():
                continue
            name = d.name.replace("__", "/", 1)
            with open(p, encoding="utf-8") as fm:
                for line in fm:
                    if line.strip():
                        m = json.loads(line)
                        w.writerow([name, m["brand"], m["marker"], m["path"], m.get("first_added_at"),
                                    m.get("first_added_sha")])

    counts = {}
    for name in ("commits.jsonl", "prs.jsonl", "features.jsonl"):
        counts[name] = _concat_jsonl((d / name for d in repo_dirs), out / name)

    any_agent = [s for s in ok if (s.get("adoption") or {}).get("brands")]
    report = {
        "repos_total": len(summaries),
        "repos_ok": len(ok),
        "repos_failed": len(summaries) - len(ok),
        "repos_eligible": len(eligible),
        "repos_with_any_agent": len(any_agent),
        "repos_with_primary_agent": sum(1 for s in ok if (s.get("adoption") or {}).get("primary_brands")),
        "eligible_with_any_agent": sum(1 for s in eligible if (s.get("adoption") or {}).get("brands")),
        "brands": {b: t["repos"] for b, t in sorted(all_table.items(), key=lambda kv: -kv[1]["repos"])},
        "rows": counts,
    }
    with open(out / "corpus_summary.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return report


def format_report(report: Dict[str, Any]) -> str:
    ok = report["repos_ok"] or 1
    lines = [
        f"Repos processed: {report['repos_ok']} ok, {report['repos_failed']} failed "
        f"({report['repos_eligible']} meet every eligibility rule)",
        f"Repos with any agent trace: {report['repos_with_any_agent']} "
        f"({100 * report['repos_with_any_agent'] / ok:.1f}%); "
        f"with one of the five primary brands: {report['repos_with_primary_agent']}",
    ]
    for brand, n in report["brands"].items():
        lines.append(f"  {BRAND_NAMES.get(brand, brand):16} {n:5} repos")
    rows = report["rows"]
    lines.append(f"Rows: {rows.get('commits.jsonl', 0)} commits, {rows.get('prs.jsonl', 0)} PRs, "
                 f"{rows.get('features.jsonl', 0)} feature vectors")
    return "\n".join(lines)
