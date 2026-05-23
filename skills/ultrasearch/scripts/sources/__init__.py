"""
sources — pluggable client modules for ultrasearch v2 profiles.

Each `<id>.py` exposes an async `fetch(query, *, config, max_items, **kwargs)`
coroutine returning a list[Candidate]. The profile YAML lists source ids
plus weights; orchestrate.py fans out concurrently across the active set.

Stage 1 ships: github, stackex, hn, deps_dev, pypi, docs_crawl.
Academic-profile sources (openalex/s2/arxiv/crossref/europepmc/core) remain
in discover.py — academic dispatch bypasses this registry.
"""
from __future__ import annotations

import importlib
from typing import Any, Awaitable, Callable

from _common import log

from ._base import Candidate, SourceError

# Map source id -> module path under sources package.
_SOURCE_MODULES: dict[str, str] = {
    "github":     "sources.github",
    "stackex":    "sources.stackex",
    "hn":         "sources.hn",
    "deps_dev":   "sources.deps_dev",
    "pypi":       "sources.pypi",
    "docs_crawl": "sources.docs_crawl",
}


def list_sources() -> list[str]:
    """Sorted list of available source ids."""
    return sorted(_SOURCE_MODULES.keys())


def load_source(source_id: str) -> Callable[..., Awaitable[list[Candidate]]]:
    """Resolve a source id to its async fetch() function. Raises SourceError
    on unknown id or import failure."""
    mod_path = _SOURCE_MODULES.get(source_id)
    if mod_path is None:
        raise SourceError(f"unknown source: {source_id!r}")
    try:
        mod = importlib.import_module(mod_path)
    except ImportError as e:
        raise SourceError(f"failed to import {mod_path}: {e}") from e
    fetch = getattr(mod, "fetch", None)
    if fetch is None:
        raise SourceError(f"{mod_path} has no fetch() function")
    return fetch


__all__ = ["Candidate", "SourceError", "list_sources", "load_source"]
