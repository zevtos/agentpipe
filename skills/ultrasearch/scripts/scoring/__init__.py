"""
scoring — per-profile quality scoring functions.

Each profile YAML names its scoring via `scoring: scoring/<file>.py:<func>`.
The orchestrator imports + invokes the function passing the candidate dict
(Candidate.to_dict()) and returns a float in [0, 1].

Stage 1 ships academic.py (re-export of v1 Q-score), dev.py, docs.py.
"""
from __future__ import annotations

import importlib
from typing import Any, Callable

from _common import log


class ScoringError(Exception):
    """Raised when a scoring reference is malformed or fails to import."""


def load_scorer(ref: str) -> Callable[[dict], float]:
    """Resolve a 'scoring/<file>.py:<func>' reference to a callable.

    Stage 1 ships only file-style refs. We translate `scoring/dev.py:score_dev`
    to the module path `scoring.dev` + attribute `score_dev`.
    """
    if ":" not in ref:
        raise ScoringError(f"invalid scoring ref (missing ':'): {ref!r}")
    path, fn_name = ref.rsplit(":", 1)
    if not path.startswith("scoring/") or not path.endswith(".py"):
        raise ScoringError(f"unsupported scoring path: {path!r}")
    mod_name = path[len("scoring/"):-len(".py")]
    if "/" in mod_name:
        raise ScoringError(f"nested scoring modules not supported: {mod_name!r}")
    try:
        mod = importlib.import_module(f"scoring.{mod_name}")
    except ImportError as e:
        raise ScoringError(f"cannot import scoring.{mod_name}: {e}") from e
    fn = getattr(mod, fn_name, None)
    if fn is None:
        raise ScoringError(f"scoring.{mod_name} has no '{fn_name}'")
    return fn


def safe_int(v: Any) -> int:
    """Coerce ``v`` to a non-negative int; return 0 for None, non-numeric,
    or negative values. Shared by per-profile scorers (dev/docs) so they
    can read sloppy GitHub/HN/PyPI int fields without retrying try/except."""
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def safe_score(scorer: Callable[[dict], float], candidate: dict) -> float:
    """Invoke a scorer, clamp to [0, 1], swallow exceptions."""
    try:
        v = float(scorer(candidate))
    except Exception as e:
        log(f"scoring error on {candidate.get('external_id')}: {e!r}; using 0.0")
        return 0.0
    if v != v:  # NaN
        return 0.0
    return max(0.0, min(1.0, v))


__all__ = ["ScoringError", "load_scorer", "safe_int", "safe_score"]
