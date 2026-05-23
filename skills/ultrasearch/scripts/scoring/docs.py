"""
scoring/docs.py — docs-profile scoring.

Heuristics:
  source-of-discovery prior (llms.txt > sitemap > crawl4ai > trafilatura)
  page-type heuristic (title keywords)
  word-count plateau (very short pages downweighted, very long undecided)
  llms.txt-presence-on-domain bonus
"""
from __future__ import annotations

import math
from typing import Any


def _safe_int(v: Any) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _source_prior(src: str) -> float:
    """How trustworthy is the discovery source?"""
    return {
        "llms.txt":     1.0,
        "sitemap":      0.7,
        "crawl4ai":     0.6,
        "trafilatura": 0.5,
    }.get(src, 0.4)


def _page_type_bonus(title: str) -> float:
    t = (title or "").lower()
    if any(kw in t for kw in ("getting started", "introduction", "overview", "quickstart")):
        return 0.2
    if any(kw in t for kw in ("api reference", "reference", "spec", "specification")):
        return 0.18
    if any(kw in t for kw in ("guide", "tutorial", "walkthrough", "how to")):
        return 0.12
    if any(kw in t for kw in ("changelog", "release notes", "migration")):
        return 0.06
    return 0.0


def _length_score(words: int) -> float:
    if words <= 50:
        return 0.2
    if words >= 4000:
        return 0.85
    # smooth ramp
    return 0.2 + (math.log1p(words) / math.log1p(4000)) * 0.6


def score_docs(candidate: dict[str, Any]) -> float:
    extras = candidate.get("extras") or {}
    src = (extras.get("source") or "").lower()
    title = candidate.get("title") or ""

    text = candidate.get("abstract") or ""
    word_count = len(text.split())

    prior = _source_prior(src) * 0.5
    pt_bonus = _page_type_bonus(title)
    length = _length_score(word_count) * 0.3
    return max(0.0, min(1.0, prior + pt_bonus + length))
