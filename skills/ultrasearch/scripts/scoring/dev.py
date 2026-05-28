"""
scoring/dev.py — quality score for dev-profile candidates.

Signals depend on the source:
  github  → stars (log), star velocity (none yet in Stage 1, use recency),
            last commit recency, archived flag, license presence,
            issues open ratio (proxy via open_issues / max(1, stars)).
  stackex → score, accepted answer, recency.
  hn      → points + comment volume, recency.
  deps_dev / pypi → downloads_last_month (log), has license, version sanity.

The function dispatches by `source_id`. Unknown sources get a neutral 0.3.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from scoring import safe_int as _safe_int


def _log_norm(value: int, ceiling_log: float = 11.5) -> float:
    """log1p(value) / ceiling_log (default 11.5 ≈ log1p(99000))."""
    return min(1.0, math.log1p(max(0, value)) / ceiling_log)


def _recency_from_days(days: int | None, half_life_days: int = 180) -> float:
    if days is None or days < 0:
        return 0.5
    return math.exp(-days / half_life_days)


def _score_github(c: dict[str, Any]) -> float:
    extras = c.get("extras") or {}
    stars = _safe_int(extras.get("stars"))
    archived = bool(extras.get("archived"))
    last_commit_days = extras.get("last_commit_days")
    open_issues = _safe_int(extras.get("open_issues"))
    has_license = bool(extras.get("license"))

    star_score = _log_norm(stars, ceiling_log=11.5)  # 100k★ → 1.0
    recency = _recency_from_days(last_commit_days, half_life_days=180)
    health = 0.1 if archived else 1.0
    # crude "issues per star" — low is good
    iratio = open_issues / max(1, stars)
    issues_score = math.exp(-iratio * 10.0)  # 1 open issue per 10★ → ~0.37

    base = (
        0.30 * star_score
        + 0.30 * recency
        + 0.15 * health
        + 0.10 * issues_score
        + 0.05 * (1.0 if has_license else 0.0)
    )
    # cap base at 0.90 so newcomer projects can never dominate by metadata alone
    return max(0.0, min(1.0, base + (0.10 if stars > 1000 else 0.0)))


def _score_stackex(c: dict[str, Any]) -> float:
    extras = c.get("extras") or {}
    score = _safe_int(extras.get("score"))
    accepted = bool(extras.get("accepted_answer"))
    creation_ts = extras.get("creation_date")
    # crude age in days
    age_days = 1000
    if creation_ts:
        try:
            age_sec = max(0.0, datetime.now(timezone.utc).timestamp() - float(creation_ts))
            age_days = age_sec / 86400.0
        except (TypeError, ValueError):
            pass
    age_norm = math.exp(-age_days / 365.0)
    rep_score = _log_norm(_safe_int(extras.get("owner_reputation")), ceiling_log=11.5)

    # SE-style: score weighted, accepted bonus, recency bonus, rep contribution
    s = (score + (5.0 if accepted else 0.0)) / (math.sqrt(max(age_days, 1.0)) + 1.0)
    s = min(1.0, s / 5.0)  # 25 votes accepted recent → ~1.0
    return max(0.0, min(1.0, 0.7 * s + 0.2 * age_norm + 0.1 * rep_score))


def _score_hn(c: dict[str, Any]) -> float:
    extras = c.get("extras") or {}
    points = _safe_int(extras.get("points"))
    comments = _safe_int(extras.get("num_comments"))
    created_at_i = extras.get("created_at_i")
    age_days = 1000.0
    if created_at_i:
        try:
            age_sec = max(0.0, datetime.now(timezone.utc).timestamp() - float(created_at_i))
            age_days = age_sec / 86400.0
        except (TypeError, ValueError):
            pass
    age_norm = math.exp(-age_days / 365.0)
    pt_score = _log_norm(points, ceiling_log=8.0)   # 3000 points → 1.0
    c_score = _log_norm(comments, ceiling_log=7.0)  # 1100 comments → 1.0
    return max(0.0, min(1.0, 0.5 * pt_score + 0.3 * c_score + 0.2 * age_norm))


def _score_pkg(c: dict[str, Any]) -> float:
    """deps_dev + pypi share the same scoring shape."""
    extras = c.get("extras") or {}
    dl = _safe_int(extras.get("downloads_last_month"))
    has_license = bool(extras.get("license"))
    version = extras.get("version") or extras.get("latest_version") or ""
    is_default = bool(extras.get("is_default", True))
    dl_score = _log_norm(dl, ceiling_log=13.5)  # 700k dl → ~1.0
    license_bonus = 0.1 if has_license else 0.0
    default_bonus = 0.05 if is_default else 0.0
    version_bonus = 0.05 if version and not version.startswith("0.") else 0.0
    return max(0.0, min(1.0, 0.7 * dl_score + license_bonus + default_bonus + version_bonus))


def _score_doc(c: dict[str, Any]) -> float:
    """Docs-profile candidate; dev's view is "is this an official-looking doc?"."""
    extras = c.get("extras") or {}
    src = extras.get("source") or ""
    title = (c.get("title") or "").lower()
    base = 0.4
    if src == "llms.txt":
        base += 0.3  # curated by site owner
    elif src == "sitemap":
        base += 0.15
    elif src == "crawl4ai":
        base += 0.1
    if any(kw in title for kw in ("guide", "tutorial", "reference", "introduction", "getting started")):
        base += 0.15
    return max(0.0, min(1.0, base))


_DISPATCH: dict[str, callable] = {
    "github": _score_github,
    "stackex": _score_stackex,
    "hn": _score_hn,
    "deps_dev": _score_pkg,
    "pypi": _score_pkg,
    "docs_crawl": _score_doc,
}


def score_dev(candidate: dict[str, Any]) -> float:
    """Dispatch to the per-source dev scorer. Unknown sources → 0.3 (neutral)."""
    src = (candidate.get("source_id") or "").lower()
    fn = _DISPATCH.get(src)
    if fn is None:
        return 0.3
    return fn(candidate)
