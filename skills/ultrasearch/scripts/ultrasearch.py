#!/usr/bin/env python3
"""
ultrasearch.py — main CLI orchestrator (Stage 1).

Pipeline (research §13 line 268; 01_stage1.md §7):
    load_env_vars  →  ensure_corpus_open
    discover (async, concurrent fan-out — OpenAlex + S2 + arXiv)
    dedup  →  truncate to --max-papers
    IF NOT --no-fetch:
        fetch_many (async, sem=4) → list[FetchResult]
        for each fetched PDF: parse_pdf (subprocess via ensure_env.py)
        for each (candidate, parse_result): index_paper (serial, BEGIN IMMEDIATE)
    ELSE:
        for each candidate: upsert as abstract-only chunk
    retrieve.topk(query, k=--top-k)
    IF NOT --no-synthesize:
        write markdown report to stdout / --out

Exit codes:
    0  success
    1  partial failure (e.g. 0 PDFs but report emitted on abstracts)
    2  configuration error (missing env var, broken venv)
    3  network failure — all 3 discovery sources returned 0
    4  pipeline crash
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# discover/fetch/index/retrieve/synthesize live next to this file (.pth-injected
# into the venv site-packages by ensure_env.py).
from _common import (
    data_dir,
    emit_failure,
    get_env,
    iso_now,
    json_stdout,
    log,
    skill_dir,
)
import discover as _discover
import fetch as _fetch
import index as _index
import retrieve as _retrieve
import synthesize as _synthesize
import traverse as _traverse
import quality as _quality
import zotero as _zotero
import render_graph as _render_graph
import translate as _translate


PARSE_SCRIPT = Path(__file__).resolve().parent / "parse.py"
PARSE_TIMEOUT_S = 180


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ultrasearch",
        description="Thesis-level literature research — Stage 1 (OpenAlex+S2+arXiv "
                    "→ Unpaywall/arXiv PDF cascade → pymupdf4llm → SPECTER → sqlite-vec).",
    )
    p.add_argument("query", help="research topic (quoted, multilingual OK)")
    p.add_argument("--max-papers", type=int, default=30,
                   help="cap on papers fetched + parsed + indexed (default: 30)")
    p.add_argument("--max-per-source", type=int, default=50,
                   help="cap per discovery source before dedup (default: 50)")
    p.add_argument("--out", type=Path, default=None,
                   help="write report to file; default stdout")
    p.add_argument("--no-fetch", action="store_true",
                   help="skip PDF download and parsing; index abstracts only")
    p.add_argument("--no-synthesize", action="store_true",
                   help="emit pipeline JSON summary instead of markdown report")
    p.add_argument("--top-k", type=int, default=20,
                   help="number of chunks for synthesis (default: 20)")
    # Stage 2 flags
    p.add_argument("--depth", choices=("shallow", "default", "deep"),
                   default="default",
                   help="shallow=Stage 1 (no traversal); default=1 hop "
                        "PaperQA2 traversal; deep=2 hops (Stage 2)")
    p.add_argument("--export-zotero", action="store_true",
                   help="write .bib next to report; push to Zotero if "
                        "ZOTERO_API_KEY+ZOTERO_USER_ID set")
    p.add_argument("--bib-out", type=Path, default=None,
                   help="path to .bib (default: <out>.bib)")
    p.add_argument("--refresh-retractions", action="store_true",
                   help="pull latest Retraction Watch CSV before marking")
    p.add_argument("--no-docling", action="store_true",
                   help="disable docling fallback on math/table-heavy pages")
    p.add_argument("--sources", default=None,
                   help="comma-separated subset of "
                        "{openalex,s2,arxiv,crossref,europepmc,core,"
                        "datacite,hf,kiberleninka,oatd,base}")
    # Stage 3 flags
    # v2 NOTE: --profile now selects pipeline routing (academic/dev/docs/...).
    # The legacy v1 retrieval-depth flag was renamed to --retrieval-profile to
    # avoid the collision. Old usage --profile=fast|full is detected and shimmed
    # in main() with a deprecation warning.
    p.add_argument("--profile", default=None,
                   help="pipeline profile: auto (classifier picks), academic "
                        "(v1 pipeline), dev, docs, ... See profiles/ for list. "
                        "Default: academic. (Old --profile=fast|full is shimmed.)")
    p.add_argument("--retrieval-profile", choices=("fast", "full"), default="fast",
                   help="fast=single-vec retrieval (Stage 1+2); "
                        "full=3-stage retrieval + multi-section synth (Stage 3). "
                        "Academic profile only.")
    p.add_argument("--profiles-dir", type=Path, default=None,
                   help="override the profile YAML directory (default: <skill>/profiles)")
    p.add_argument("--no-classifier", action="store_true",
                   help="skip classifier subagent; require --profile or use keyword fallback")
    p.add_argument("--output-template", default=None,
                   help="override the profile's default output template "
                        "(filename in templates/, e.g. adr.md.j2)")
    p.add_argument("--lang", choices=("auto", "en", "ru"), default="auto",
                   help="query language; if non-English, ultrasearch generates "
                        "an EN parallel query for English-only sources")
    p.add_argument("--render-graph", action="store_true",
                   help="embed Mermaid citation graph in the report (Stage 3)")
    p.add_argument("--grey", action="store_true",
                   help="opt-in: sci-hub fallback for unreachable OA PDFs "
                        "(LEGAL GRAY ZONE — read --grey disclaimer first)")
    p.add_argument("--db", type=Path, default=None,
                   help="override corpus.db path; default <skill_dir>/data/corpus.db")
    p.add_argument("--json", action="store_true",
                   help="emit pipeline statistics as JSON to stderr at end")
    p.add_argument("--quiet", action="store_true",
                   help="suppress per-step log lines on stderr")
    return p


def _check_env(quiet: bool) -> dict[str, str | None]:
    keys = {
        "OPENALEX_API_KEY": get_env("OPENALEX_API_KEY"),
        "OPENALEX_EMAIL":   get_env("OPENALEX_EMAIL"),
        "UNPAYWALL_EMAIL":  get_env("UNPAYWALL_EMAIL"),
        "S2_API_KEY":       get_env("S2_API_KEY"),
    }
    if not quiet:
        for k, v in keys.items():
            present = "set" if v else "UNSET"
            log(f"env {k}: {present}")
        if not keys["OPENALEX_API_KEY"]:
            log("OPENALEX_API_KEY missing — running in legacy mode (mandatory after 13 Feb 2026)")
        if not keys["UNPAYWALL_EMAIL"]:
            log("UNPAYWALL_EMAIL missing — Unpaywall fetch tier disabled (only arXiv direct will work)")
    return keys


def _run_parse_subprocess(pdf_path: str, candidate_key: str) -> dict[str, Any]:
    """Run parse.py in a subprocess via the venv python. Parse the one-line
    JSON stdout."""
    py = sys.executable
    cmd = [py, str(PARSE_SCRIPT), pdf_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=PARSE_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "parse_timeout", "candidate_key": candidate_key}
    out = r.stdout.strip().splitlines()
    if not out:
        return {"ok": False, "reason": f"parse_no_stdout (rc={r.returncode})",
                "candidate_key": candidate_key, "stderr": r.stderr[-2000:]}
    try:
        result = json.loads(out[-1])
    except json.JSONDecodeError as e:
        return {"ok": False, "reason": f"parse_bad_json: {e}",
                "candidate_key": candidate_key, "stdout": out[-1][:500]}
    result["candidate_key"] = candidate_key
    return result


async def _pipeline(args) -> dict[str, Any]:
    """Run the full Stage 1 pipeline. Returns a stats dict."""
    t_start = time.monotonic()
    stats: dict[str, Any] = {
        "query": args.query,
        "started_at": iso_now(),
        "stage": 1,
        "ok": True,
        "warnings": [],
    }

    # Stage 3: detect query language; if non-English, generate EN parallel
    query_used = args.query
    query_lang = "en"
    if args.lang in ("auto", "ru"):
        det = _translate.detect_lang(args.query)
        query_lang = det.get("lang", "en") if args.lang == "auto" else args.lang
        stats["language"] = {"detected": query_lang,
                             "confidence": det.get("confidence", 0.0)}
        if query_lang != "en":
            exp = _translate.expand_query(args.query, target_lang="en")
            en_query = exp.get("en")
            if en_query and en_query != args.query:
                # Use the EN query for English-only sources; keep the original
                # for synthesis output language consistency
                query_used = en_query
                stats["language"]["en_query"] = en_query

    # 1. discover
    t0 = time.monotonic()
    if args.sources:
        sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    else:
        # Stage 3 default: include all stable sources; RU sources via --sources
        sources = _discover.DEFAULT_SOURCES
    disc = await _discover.discover(
        query_used,
        max_per_source=args.max_per_source,
        sources=sources,
    )
    stats["discover"] = {
        "n_candidates": disc.get("n_candidates", 0),
        "n_unique":     disc.get("n_unique", 0),
        "per_source":   disc.get("per_source", {}),
        "warnings":     disc.get("warnings", []),
        "seconds":      round(time.monotonic() - t0, 2),
    }
    candidates = disc.get("candidates") or []
    if not candidates:
        stats["ok"] = False
        stats["error"] = "discover_empty"
        stats["exit_code"] = 3
        return stats

    # 2. truncate
    if args.max_papers and len(candidates) > args.max_papers:
        candidates = candidates[: args.max_papers]
    stats["n_papers_after_truncate"] = len(candidates)

    # 3. open corpus
    try:
        con = _index.open_corpus(args.db)
    except RuntimeError as e:
        stats["ok"] = False
        stats["error"] = f"corpus_open: {e}"
        stats["exit_code"] = 2
        return stats

    # 4. fetch + parse + index, OR abstracts-only
    fetched_paths: dict[str, dict[str, Any]] = {}
    parsed: dict[str, dict[str, Any]] = {}
    indexed_results: list[dict[str, Any]] = []

    if args.no_fetch:
        log("--no-fetch: indexing abstracts only", prefix="ultrasearch")
        for c in candidates:
            abs_ = (c.get("abstract") or "").strip()
            full_text = abs_ if abs_ else ""
            ir = _index.index_paper(con, c, full_text)
            indexed_results.append(ir)
        stats["fetch"] = {"skipped": True}
        stats["parse"] = {"skipped": True}
    else:
        # fetch (Stage 3: --grey opt-in passes through to sci-hub tier)
        if args.grey:
            log(_fetch.GREY_DISCLAIMER, prefix="ultrasearch")
        t0 = time.monotonic()
        fetches = await _fetch.fetch_many(candidates, concurrency=4, grey=args.grey)
        stats["fetch"] = {
            "n_attempted": len(candidates),
            "n_ok":        sum(1 for f in fetches if f.get("ok")),
            "n_no_oa":     sum(1 for f in fetches if not f.get("ok")),
            "seconds":     round(time.monotonic() - t0, 2),
        }
        for f in fetches:
            if f.get("ok") and f.get("pdf_path"):
                fetched_paths[f["candidate_key"]] = f

        # parse (subprocess per PDF; serial in Stage 1 — pymupdf4llm releases
        # the GIL but the subprocess invocation is the safety net)
        t0 = time.monotonic()
        n_parse_ok = 0
        n_parse_fail = 0
        for c in candidates:
            key = _discover._candidate_key(c)
            f = fetched_paths.get(key)
            if not f:
                continue
            pr = _run_parse_subprocess(f["pdf_path"], key)
            parsed[key] = pr
            if pr.get("ok"):
                n_parse_ok += 1
            else:
                n_parse_fail += 1
        stats["parse"] = {
            "n_ok":      n_parse_ok,
            "n_fail":    n_parse_fail,
            "seconds":   round(time.monotonic() - t0, 2),
        }

        # index — serial writes (sqlite single-writer)
        t0 = time.monotonic()
        for c in candidates:
            key = _discover._candidate_key(c)
            f = fetched_paths.get(key)
            pr = parsed.get(key)
            full_text = (pr or {}).get("text") or ""
            if not full_text and (c.get("abstract") or "").strip():
                full_text = c["abstract"]
            ir = _index.index_paper(
                con, c, full_text,
                pdf_path=(f or {}).get("pdf_path"),
                pdf_sha256=(f or {}).get("sha256"),
            )
            indexed_results.append(ir)
        stats["index"] = {
            "n_papers": len(indexed_results),
            "n_chunks_total": sum(int(r.get("n_chunks") or 0) for r in indexed_results),
            "n_failed":       sum(1 for r in indexed_results if not r.get("ok")),
            "seconds":        round(time.monotonic() - t0, 2),
        }

    # 5. retrieve (--retrieval-profile full triggers cross-encoder + RCS-cache)
    t0 = time.monotonic()
    if args.retrieval_profile == "full":
        hits = _retrieve.retrieve_3stage(con, args.query, final_k=args.top_k)
    else:
        hits = _retrieve.topk(con, args.query, k=args.top_k)
    stats["retrieve"] = {
        "retrieval_profile": args.retrieval_profile,
        "n_hits": len(hits),
        "seconds": round(time.monotonic() - t0, 2),
    }

    # 5a. (Stage 2) citation traversal
    traversal_candidates: list[dict[str, Any]] = []
    if args.depth in ("default", "deep") and hits:
        t0 = time.monotonic()
        seeds: list[_traverse.TraversalSeed] = []
        for h in hits[:10]:
            seeds.append({
                "paper_id": h["paper_id"],
                "doi": h.get("doi"),
                "title": h.get("title", ""),
                "score": float(h.get("score") or 0.0),
            })
        max_hops = 1 if args.depth == "default" else 2
        try:
            trv = await _traverse.traverse_citations(
                con, seeds, max_hops=max_hops,
            )
            stats["traverse"] = {
                "n_seeds": trv.get("n_seeds", 0),
                "n_raw_candidates": trv.get("n_raw_candidates", 0),
                "n_after_specter_gate": trv.get("n_after_specter_gate", 0),
                "n_final": trv.get("n_final", 0),
                "api_calls": trv.get("api_calls", {}),
                "warnings": trv.get("warnings", []),
                "seconds": round(time.monotonic() - t0, 2),
            }
            traversal_candidates = trv.get("candidates") or []
        except Exception as e:
            log(f"traverse failed: {type(e).__name__}: {e}", prefix="ultrasearch")
            stats["traverse"] = {"error": f"{type(e).__name__}: {e}",
                                 "seconds": round(time.monotonic() - t0, 2)}

    # 5b. (Stage 2) Retraction Watch
    retraction_lookup: dict[str, dict[str, Any]] = {}
    if args.refresh_retractions:
        t0 = time.monotonic()
        try:
            rr = _quality.refresh_retractions()
            stats["quality_refresh"] = {
                "ok": rr.get("ok"),
                "inserted": rr.get("inserted", 0),
                "updated": rr.get("updated", 0),
                "csv_rows": rr.get("csv_rows", 0),
                "seconds": round(time.monotonic() - t0, 2),
            }
        except Exception as e:
            log(f"refresh_retractions failed: {e}", prefix="ultrasearch")
            stats["quality_refresh"] = {"error": str(e)}
    # Mark retractions for current corpus regardless
    try:
        mark = _quality.mark_retractions(con)
        stats["quality_mark"] = {"n_marked": mark.get("n_marked", 0)}
        # Build lookup so synthesize can footnote
        cur = con.execute(
            "SELECT r.doi, r.retraction_date, r.reason "
            "FROM retractions r JOIN papers p ON LOWER(p.doi) = LOWER(r.doi)"
        )
        for row in cur:
            retraction_lookup[row["doi"]] = {
                "retraction_date": row["retraction_date"],
                "reason": row["reason"],
            }
    except Exception as e:
        log(f"mark_retractions failed: {e}", prefix="ultrasearch")

    con.close()

    # 6. synthesize
    if args.no_synthesize:
        out_payload = {**stats, "hits_preview": [
            {"paper_id": h["paper_id"], "title": h["title"], "score": h["score"]}
            for h in hits[:5]
        ]}
        json_stdout(out_payload)
    else:
        report = _synthesize.synthesize(
            args.query, hits,
            retraction_lookup=retraction_lookup or None,
            traversal_candidates=traversal_candidates or None,
        )
        # Stage 3: append Mermaid citation graph if requested
        if args.render_graph and hits:
            try:
                con3 = _index.open_corpus(args.db)
                try:
                    paper_ids = [h["paper_id"] for h in hits]
                    graph = _render_graph.render(con3, paper_ids)
                finally:
                    con3.close()
                if graph:
                    report = (report.rstrip() + "\n\n## Citation graph\n\n"
                              "```mermaid\n" + graph + "\n```\n")
                    stats["citation_graph"] = {"nodes": len(paper_ids),
                                               "rendered": True}
            except Exception as e:
                log(f"render_graph failed: {e}", prefix="ultrasearch")
                stats["citation_graph"] = {"error": str(e)}
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(report, encoding="utf-8")
            log(f"report written to {args.out}", prefix="ultrasearch")
        else:
            sys.stdout.write(report)
        v = _synthesize.validate_report(report)
        stats["report"] = v

    # 7. (Stage 2) export bib / Zotero
    if args.export_zotero and hits:
        t0 = time.monotonic()
        if args.bib_out:
            bib_path = args.bib_out
        elif args.out:
            bib_path = args.out.with_suffix(".bib")
        else:
            bib_path = Path(os.getcwd()) / "ultrasearch-refs.bib"
        try:
            con2 = _index.open_corpus(args.db)
            try:
                paper_ids = [h["paper_id"] for h in hits]
                push = get_env("ZOTERO_API_KEY") is not None
                ex = _zotero.export(con2, paper_ids,
                                    bib_path=bib_path,
                                    push_zotero=push)
            finally:
                con2.close()
            stats["export"] = {
                "ok": ex.get("ok"),
                "bib_path": ex.get("bib_path"),
                "n_entries": ex.get("n_entries"),
                "n_pushed_to_zotero": ex.get("n_pushed_to_zotero", 0),
                "warnings": ex.get("warnings", []),
                "seconds": round(time.monotonic() - t0, 2),
            }
        except Exception as e:
            log(f"export failed: {e}", prefix="ultrasearch")
            stats["export"] = {"error": str(e)}

    stats["total_seconds"] = round(time.monotonic() - t_start, 2)
    stats["finished_at"] = iso_now()
    return stats


# ---------- v2 profile dispatch ----------

_V1_RETRIEVAL_VALUES = {"fast", "full"}
_V2_PROFILE_DEFAULT = "academic"


def _resolve_profile(args) -> tuple[str, str]:
    """Resolve --profile to (pipeline_profile, dispatch_target).

    Returns:
        pipeline_profile: 'academic' | 'dev' | 'docs' | ... (which YAML to load)
        dispatch_target:  'academic' (v1 _pipeline) | 'v2' (orchestrate.run_pipeline)

    Handles the v1 collision: --profile=fast|full are remapped to
    --retrieval-profile and a deprecation warning is emitted. The pipeline
    profile defaults to academic.
    """
    raw = args.profile
    if raw is None:
        # no --profile given — keep v1 behavior (academic pipeline)
        return _V2_PROFILE_DEFAULT, "academic"

    if raw in _V1_RETRIEVAL_VALUES:
        log(f"DEPRECATION: --profile={raw} is the v1 retrieval-depth flag and "
            f"has been renamed to --retrieval-profile={raw} in v2. Treating "
            f"as --profile=academic --retrieval-profile={raw}.")
        args.retrieval_profile = raw
        return "academic", "academic"

    if raw == "auto":
        # Classifier path. For Stage 1 we delegate to classifier.py keyword
        # fallback; subagent integration is Stage 1.5.
        try:
            import classifier as _classifier
        except ImportError as e:
            log(f"classifier import failed ({e}); defaulting to academic")
            return "academic", "academic"
        result = _classifier.classify(args.query, no_classifier=args.no_classifier)
        log(f"classifier: primary={result.primary_profile} "
            f"recursive={result.is_recursive} source={result.source} "
            f"confidence={result.confidence:.2f}")
        chosen = _resolve_to_available(result.primary_profile, args.profiles_dir)
        if chosen == "academic":
            return "academic", "academic"
        return chosen, "v2"

    if raw == "academic":
        return "academic", "academic"

    # Any other named profile → v2 dispatch (verify YAML exists)
    resolved = _resolve_to_available(raw, args.profiles_dir, fall_back_silent=False)
    if resolved == "academic":
        return "academic", "academic"
    return resolved, "v2"


_PROFILE_NAME_RE = __import__("re").compile(r"^[a-z][a-z0-9_]*$")


def _resolve_to_available(
    chosen: str,
    profiles_dir: Path | None,
    *,
    fall_back_silent: bool = True,
) -> str:
    """Verify `chosen` profile has a YAML on disk; fall back to academic if not.

    Classifier's 8 PROFILE_KEYS (academic/dev/product/startup/regulatory/docs/
    community/meta) are not all shipped as YAMLs in Stage 1. When the classifier
    picks an unshipped profile, we degrade gracefully to academic with a log
    line rather than crashing in orchestrate.run_pipeline.
    """
    # Belt-and-braces: if `chosen` ever comes from an untrusted source
    # (subagent JSON via $ULTRASEARCH_CLASSIFIER_RESULT) reject anything
    # that doesn't match the canonical profile-name regex BEFORE doing any
    # filesystem work. load_profile() has the same check, but keep this
    # local so future refactors can't accidentally bypass it.
    if not isinstance(chosen, str) or not _PROFILE_NAME_RE.match(chosen):
        log(f"profile name rejected (regex): {chosen!r}; using academic")
        return "academic"
    try:
        from profile import list_profiles
        available = set(list_profiles(profiles_dir))
    except Exception as e:
        log(f"profile listing failed ({e}); using academic")
        return "academic"
    if chosen in available:
        return chosen
    if not fall_back_silent:
        log(f"profile {chosen!r} has no YAML in profiles/; available: "
            f"{sorted(available)}. Falling back to academic.")
    else:
        log(f"classifier picked {chosen!r} but no YAML exists; "
            f"falling back to academic. Available: {sorted(available)}")
    return "academic"


def main() -> int:
    args = _build_parser().parse_args()
    _check_env(args.quiet)

    try:
        pipeline_profile, dispatch_target = _resolve_profile(args)
    except Exception as e:
        log(f"profile resolution failed: {e}", prefix="ultrasearch")
        return 2

    log(f"dispatch: profile={pipeline_profile} target={dispatch_target}")

    if dispatch_target == "v2":
        # v2 generic flat pipeline — orchestrate.run_pipeline owns it
        try:
            import orchestrate as _orchestrate
        except ImportError as e:
            log(f"orchestrate import failed: {e}", prefix="ultrasearch")
            return 4
        try:
            result = asyncio.run(_orchestrate.run_pipeline(
                args.query,
                pipeline_profile,
                profiles_dir=args.profiles_dir,
                max_items=args.max_papers,
                top_k=args.top_k,
                out_path=args.out,
                output_template=args.output_template,
            ))
        except KeyboardInterrupt:
            emit_failure("interrupted")
            return 130
        except Exception as e:
            log(f"v2 pipeline crashed: {type(e).__name__}: {e}", prefix="ultrasearch")
            return 4
        # Emit markdown to stdout if no --out
        if args.out is None and result.get("markdown"):
            sys.stdout.write(result["markdown"])
        if args.json:
            public = {k: v for k, v in result.items() if k != "markdown"}
            log(json.dumps(public, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    # academic (v1) pipeline
    try:
        stats = asyncio.run(_pipeline(args))
    except KeyboardInterrupt:
        emit_failure("interrupted")
        return 130
    except Exception as e:
        log(f"pipeline crashed: {type(e).__name__}: {e}", prefix="ultrasearch")
        return 4

    if args.json:
        log(json.dumps(stats, ensure_ascii=False))

    if not stats.get("ok"):
        return int(stats.get("exit_code", 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
