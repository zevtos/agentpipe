"""
_base.py — Candidate dataclass + SourceError shared across source modules.

A Candidate is the normalized output of every source. Profile-specific
fields land in `extras` (a free-form dict). Required fields are kept to
the minimum that orchestrate.py needs for dedup, scoring, and indexing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any


_TOKEN_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on",
    "is", "are", "vs", "best", "library", "framework", "package",
    "alternatives", "comparison", "python", "with", "using", "review",
})


def tokenize_package_names(query: str, *, max_tokens: int = 8) -> list[str]:
    """Extract plausible package-name candidates from a free-text query.
    Drops stopwords and de-duplicates. Used by both pypi.py and deps_dev.py
    to seed their package lookups.

    Stopword set adopted from pypi.py (deps_dev.py historically lacked
    `"review"`); harmless union because no real PyPI package is named
    `review`.
    """
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", query.lower())
    out: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        if t in _TOKEN_STOPWORDS or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out[:max_tokens]


class SourceError(Exception):
    """Raised by source clients on fatal API failures (auth, malformed
    response). Transient HTTP errors are retried inside the client; if
    retries are exhausted, fetch() may return an empty list with a logged
    warning rather than raising — orchestrate.py treats empty results as
    soft failures."""


@dataclass
class Candidate:
    """Normalized output of one source's fetch().

    Required fields:
        source_id    -- which sources/<id>.py produced this candidate
        source_type  -- coarse type ('repo' | 'qa' | 'doc' | 'forum' |
                                     'page' | 'pkg' | 'filing')
        title        -- one-line human-readable title
        url          -- canonical URL (used as part of dedup key when DOI absent)
        external_id  -- stable cross-run identifier
                        ('github:owner/repo', 'so:question:12345',
                         'pypi:numpy', 'hn:story:9999')

    Optional enrichment fields:
        abstract    -- short body / description (≤ 2000 chars)
        authors     -- list of author display names
        year        -- publication / created year
        doi         -- only if cross-walked from a published paper
        score_raw   -- pre-scoring signal hints (stars, votes, downloads, …)
        extras      -- everything else (profile-specific scoring inputs,
                       e.g. {'stars': 12345, 'last_commit': '2026-04-10', …})
    """
    source_id: str
    source_type: str
    title: str
    url: str
    external_id: str

    abstract: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    score_raw: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["Candidate", "SourceError", "tokenize_package_names"]
