"""
scoring/academic.py — Q-score wrapper for the academic profile.

The v1 quality module already implements compute_q_score(); we re-export
it through a uniform `score_academic(candidate_dict) -> float` signature
so the v2 orchestrator can call it the same way as score_dev / score_docs.

For academic candidates that already passed through the v1 pipeline,
quality.compute_q_score expects the raw paper row (with citations, year,
journal_if). For v2-style Candidate dicts we map the relevant signals.
"""
from __future__ import annotations

import math
from typing import Any

from _common import RECENT_YEAR_BASELINE


def score_academic(candidate: dict[str, Any]) -> float:
    """Return a [0, 1] quality score for an academic candidate.

    Signals (any missing → use neutral default):
      * citations (extras.cited_by_count or extras.citations) → log1p / 8.0
      * year → recency weight (papers in last 5 years boosted)
      * has_doi → +0.05
      * has_abstract → +0.02
    """
    extras = candidate.get("extras") or {}

    cites = (
        extras.get("cited_by_count")
        or extras.get("citation_count")
        or extras.get("citations")
        or 0
    )
    try:
        cites = max(0, int(cites))
    except (TypeError, ValueError):
        cites = 0
    cite_score = min(1.0, math.log1p(cites) / 8.0)  # ~3000 cites → 1.0

    year = candidate.get("year")
    recency = 0.5
    if year:
        try:
            y = int(year)
            # 5-year half-life
            age = max(0, RECENT_YEAR_BASELINE - y)
            recency = math.exp(-age / 5.0)
        except (TypeError, ValueError):
            pass

    has_doi = bool(candidate.get("doi"))
    has_abstract = bool(candidate.get("abstract"))

    base = 0.6 * cite_score + 0.3 * recency
    bonus = (0.05 if has_doi else 0.0) + (0.02 if has_abstract else 0.0)
    return max(0.0, min(1.0, base + bonus))
