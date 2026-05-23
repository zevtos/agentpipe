"""
orchestrate.py — generic flat pipeline for non-academic profiles.

Pipeline:
  1. Load ProfileConfig from profiles/<name>.yaml
  2. Fan out to each source in profile.sources (asyncio.gather)
  3. Dedup candidates (by external_id, then url, then casefold(title))
  4. Score each candidate with the profile's scoring function
  5. Sort descending by (score, recency) and take top_k
  6. Render markdown via synthesize_v2 + the profile's output_template

For the academic profile, ultrasearch.py routes around this module and
invokes the v1 pipeline instead (see _run_academic_pipeline).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from _common import casefold_title, log, iso_now
from profile import load_profile, ProfileConfig, ConfigError
from sources import load_source, SourceError
from scoring import load_scorer, safe_score
from synthesize_v2 import synthesize


def _candidate_to_dict(c: Any) -> dict[str, Any]:
    """Normalize Candidate dataclass or plain dict to dict."""
    if is_dataclass(c):
        return asdict(c)
    if isinstance(c, dict):
        return dict(c)
    raise TypeError(f"unexpected candidate type: {type(c).__name__}")


def _dedup_candidates(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedup by external_id → url → casefold(title)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in cands:
        key = c.get("external_id") or c.get("url") or casefold_title(c.get("title"))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


async def _fetch_one_source(
    source_id: str,
    weight: float,
    config: dict[str, Any],
    query: str,
    max_items: int,
) -> list[dict[str, Any]]:
    try:
        fetch = load_source(source_id)
    except SourceError as e:
        log(f"orchestrate: skip {source_id}: {e}")
        return []

    try:
        raw = await fetch(query, config=config, max_items=max_items)
    except Exception as e:
        log(f"orchestrate: {source_id} fetch failed: {e!r}")
        return []

    out: list[dict[str, Any]] = []
    for c in raw or []:
        try:
            d = _candidate_to_dict(c)
        except TypeError as e:
            log(f"orchestrate: {source_id} bad candidate {e}")
            continue
        d.setdefault("source_id", source_id)
        d["_source_weight"] = weight
        out.append(d)
    return out


async def run_pipeline(
    query: str,
    profile_name: str,
    *,
    profiles_dir: Path | None = None,
    max_items: int | None = None,
    top_k: int | None = None,
    out_path: Path | None = None,
    output_template: str | None = None,
) -> dict[str, Any]:
    """Run the generic flat pipeline for `profile_name`.

    Returns a dict with: ok, profile, query, n_candidates, n_unique, top_k,
    out_path (if written), markdown (rendered text).
    """
    cfg: ProfileConfig = load_profile(profile_name, profiles_dir=profiles_dir)
    flat_defaults = cfg.flat_defaults or {}
    if max_items is None:
        max_items = int(flat_defaults.get("max_items", 50))
    if top_k is None:
        top_k = int(flat_defaults.get("top_k", 20))

    # 1. fan-out
    log(f"orchestrate: profile={cfg.name} sources={[s.id for s in cfg.sources]} max_items={max_items}")
    per_source = max(1, max_items // max(1, len(cfg.sources)))
    tasks = [
        _fetch_one_source(s.id, s.weight, s.config, query, per_source)
        for s in cfg.sources
    ]
    per_source_results = await asyncio.gather(*tasks, return_exceptions=True)

    cands: list[dict[str, Any]] = []
    for r in per_source_results:
        if isinstance(r, Exception):
            log(f"orchestrate: source task error {r!r}")
            continue
        cands.extend(r)
    n_total = len(cands)
    cands = _dedup_candidates(cands)
    log(f"orchestrate: {n_total} raw, {len(cands)} after dedup")

    # 2. score
    try:
        scorer = load_scorer(cfg.scoring_ref)
    except Exception as e:
        log(f"orchestrate: scoring load failed ({e}); using 0.5 neutral")
        scorer = None
    for c in cands:
        if scorer is not None:
            c["quality_score"] = safe_score(scorer, c)
        else:
            c["quality_score"] = 0.5
        # blend source weight (small bias) into rank
        c["_rank_score"] = c["quality_score"] * 0.9 + (c.get("_source_weight") or 0.0) * 0.1

    # 3. rank + truncate
    cands.sort(key=lambda c: (-(c.get("_rank_score") or 0.0), c.get("title") or ""))
    top = cands[:top_k]

    # 4. render
    template = output_template or cfg.output_template
    md = synthesize(query, top, template=template, out_path=out_path,
                    extra_context={"profile": cfg.name})
    log(f"orchestrate: rendered {len(md)} chars via {template}")

    return {
        "ok": True,
        "profile": cfg.name,
        "query": query,
        "template": template,
        "n_candidates": n_total,
        "n_unique": len(cands),
        "n_top": len(top),
        "out_path": str(out_path) if out_path else None,
        "generated_at": iso_now(),
        "markdown": md if not out_path else None,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="ultrasearch v2 generic flat orchestrator")
    p.add_argument("query")
    p.add_argument("--profile", required=True)
    p.add_argument("--max-items", type=int, default=None)
    p.add_argument("--top-k", type=int, default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--profiles-dir", default=None)
    p.add_argument("--template", default=None,
                   help="override the profile's default output template")
    p.add_argument("--json", action="store_true",
                   help="emit JSON result instead of markdown")
    args = p.parse_args(argv)

    profiles_dir = Path(args.profiles_dir) if args.profiles_dir else None
    out_path = Path(args.out) if args.out else None

    try:
        result = asyncio.run(run_pipeline(
            args.query,
            args.profile,
            profiles_dir=profiles_dir,
            max_items=args.max_items,
            top_k=args.top_k,
            out_path=out_path,
            output_template=args.template,
        ))
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.json:
        # Don't include rendered markdown body in JSON (can be large)
        public = {k: v for k, v in result.items() if k != "markdown"}
        print(json.dumps(public, ensure_ascii=False, indent=2))
    elif out_path is None and result.get("markdown"):
        print(result["markdown"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
