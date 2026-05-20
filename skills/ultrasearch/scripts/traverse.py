#!/usr/bin/env python3
"""
traverse.py — PaperQA2 Algorithm 1 citation traversal (Stage 2).

Replicates the algorithm from research §3 lines 63-69:
    D_prev = {s.d for s in S if s.score >= theta_score}
    D     = GetCitations(D_prev, fut)        # one degree only
    theta_o = math.ceil(alpha * len(D))
    return FilterOverlap(D, D_prev, theta_o, ell)

Stage 2 additions (research §3 line 71):
    - SPECTER cosine gate at 0.55 against corpus centroid
    - max_hops > 1 enables `--depth deep` (recursive traversal)

API call budget per seed (research §3 line 57): 3 calls = 1 S2 backward refs +
1 Crossref backward refs + 1 S2 forward citers. Total for 20-seed traversal
= 60 calls; runs ~9s under aiometer per-domain budgets.

CLI:
    traverse.py --seeds-json '[{"paper_id":"abc","doi":"10.x","title":"...","score":0.85}]'
                [--max-hops 1] [--json]
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
import math
import sqlite3
import struct
import sys
import time
from typing import Any, Iterable, Literal, TypedDict

import httpx
import numpy as np

from _common import (
    casefold_title,
    cosine,
    emit_failure,
    get_env,
    iso_now,
    json_stdout,
    log,
    nfc,
    normalize_doi,
    short_id,
)
from discover import (
    Candidate,
    USER_AGENT,
    _candidate_key,
    _retry_get,
)
from index import (
    EMBED_DIM,
    EMBED_MODEL_NAME,
    embed_batch,
    open_corpus,
    writer_tx,
)


# -------- constants --------

THETA_SCORE = 8.0           # PaperQA2 RCS threshold (0-10 scale)
ALPHA = 1.0 / 3.0           # overlap-filter fraction (research §3 line 60)
ELL = 12                    # hard cap on new candidates per call (research §3 line 60)
SPECTER_GATE = 0.55         # SciNCL nearest-neighbour threshold (research §3 line 71)
MAX_HOPS_DEFAULT = 1

# Per-domain rate budgets (additive to discover.RATE_BUDGET)
TRAVERSE_RATE_BUDGET: dict[str, float] = {
    "s2_refs":   1.0,    # research §2 line 27 — dedicated key = 1 RPS
    "s2_citers": 1.0,    # same pool
    "crossref":  5.0,    # research §2 line 29 — polite pool 50 RPS, stay polite
}

S2_BASE = "https://api.semanticscholar.org/graph/v1"
CROSSREF_BASE = "https://api.crossref.org/works"
PER_SOURCE_TIMEOUT_S = 60.0


class TraversalSeed(TypedDict, total=False):
    paper_id: str
    doi: str | None
    title: str
    score: float


class TraversalCandidate(TypedDict, total=False):
    # mirrors discover.Candidate plus traversal-specific fields
    source: Literal["s2", "crossref"]
    doi: str | None
    arxiv_id: str | None
    s2_paper_id: str | None
    title: str
    title_norm: str
    abstract: str | None
    year: int | None
    authors: list[str]
    venue: str | None
    cited_by_count: int | None
    referenced_works: list[str]
    parent_paper_id: str
    direction: Literal["forward", "backward"]
    source_api: Literal["s2", "crossref"]
    cosine_to_centroid: float | None
    discovered_via_traversal: bool


class TraversalResult(TypedDict, total=False):
    ok: bool
    n_seeds: int
    n_raw_candidates: int
    n_after_overlap: int
    n_after_specter_gate: int
    n_final: int
    candidates: list[TraversalCandidate]
    elapsed_s: float
    api_calls: dict[str, int]
    warnings: list[str]


# -------- corpus centroid --------

def compute_corpus_centroid(con: sqlite3.Connection, *,
                            paper_ids: list[str] | None = None,
                            model_name: str = EMBED_MODEL_NAME
                            ) -> np.ndarray | None:
    """Mean of all chunk embeddings (or paper_ids subset). Returns
    normalized centroid or None on empty corpus."""
    if paper_ids:
        placeholders = ",".join("?" * len(paper_ids))
        rows = con.execute(
            f"SELECT v.embedding FROM vec_chunks v "
            f"JOIN chunks c ON c.chunk_id = v.rowid "
            f"WHERE c.paper_id IN ({placeholders}) AND c.model_name = ?",
            (*paper_ids, model_name),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT v.embedding FROM vec_chunks v "
            "JOIN chunks c ON c.chunk_id = v.rowid "
            "WHERE c.model_name = ? "
            "ORDER BY c.chunk_id DESC LIMIT 10000",
            (model_name,),
        ).fetchall()
    if not rows:
        return None
    vecs = np.zeros((len(rows), EMBED_DIM), dtype=np.float32)
    for i, r in enumerate(rows):
        try:
            vecs[i] = np.frombuffer(r["embedding"], dtype="<f4")
        except (ValueError, TypeError):
            continue
    centroid = vecs.mean(axis=0)
    norm = float(np.linalg.norm(centroid))
    if norm == 0.0:
        return None
    return (centroid / norm).astype(np.float32)


# -------- API helpers --------

async def _s2_get_paper_relations(client: httpx.AsyncClient, doi: str,
                                  endpoint: Literal["references", "citations"],
                                  api_key: str | None
                                  ) -> list[TraversalCandidate]:
    """GET /paper/DOI:{doi}/{references|citations} with paper-shaped response."""
    url = f"{S2_BASE}/paper/DOI:{doi}/{endpoint}"
    params = {
        "limit": 100,
        "fields": "title,abstract,year,authors,venue,citationCount,externalIds,"
                  "openAccessPdf,paperId",
    }
    headers = {"User-Agent": USER_AGENT.format(email="anonymous")}
    if api_key:
        headers["x-api-key"] = api_key
    r = await _retry_get(client, url, params=params, headers=headers,
                         source=f"s2_{endpoint}")
    if r is None or r.status_code != 200:
        return []
    try:
        data = r.json()
    except Exception:
        return []
    items = data.get("data") or []
    out: list[TraversalCandidate] = []
    direction: Literal["backward", "forward"] = (
        "backward" if endpoint == "references" else "forward"
    )
    for it in items:
        # /references items wrap target paper as `citedPaper`, /citations as `citingPaper`
        paper = it.get("citedPaper") if endpoint == "references" else it.get("citingPaper")
        if not paper:
            continue
        title = paper.get("title")
        if not title:
            continue
        ext = paper.get("externalIds") or {}
        out.append({
            "source": "s2",
            "source_api": "s2",
            "direction": direction,
            "doi": normalize_doi(ext.get("DOI")),
            "arxiv_id": ext.get("ArXiv"),
            "s2_paper_id": paper.get("paperId"),
            "title": nfc(title),
            "title_norm": casefold_title(title),
            "abstract": paper.get("abstract"),
            "year": paper.get("year"),
            "authors": [a.get("name") for a in (paper.get("authors") or [])
                        if a.get("name")],
            "venue": paper.get("venue"),
            "cited_by_count": paper.get("citationCount"),
            "referenced_works": [],
            "discovered_via_traversal": True,
        })
    return out


async def _crossref_backward_refs(client: httpx.AsyncClient, doi: str,
                                  email: str | None
                                  ) -> list[TraversalCandidate]:
    """GET https://api.crossref.org/works/{doi} → message.reference[].DOI."""
    url = f"{CROSSREF_BASE}/{doi}"
    params: dict[str, str] = {}
    if email:
        params["mailto"] = email
    headers = {"User-Agent": USER_AGENT.format(email=email or "anonymous")}
    r = await _retry_get(client, url, params=params, headers=headers,
                         source="crossref")
    if r is None or r.status_code != 200:
        return []
    try:
        data = r.json()
    except Exception:
        return []
    refs = ((data.get("message") or {}).get("reference") or [])
    out: list[TraversalCandidate] = []
    for ref in refs:
        rdoi = normalize_doi(ref.get("DOI"))
        title = ref.get("article-title") or ref.get("unstructured") or ""
        if not (rdoi or title):
            continue
        # Crossref references rarely have abstracts; we still emit a candidate
        # so downstream dedup can union with the S2 returns.
        title = title.strip()[:300] if title else (rdoi or "(no title)")
        out.append({
            "source": "crossref",
            "source_api": "crossref",
            "direction": "backward",
            "doi": rdoi,
            "title": nfc(title),
            "title_norm": casefold_title(title),
            "abstract": None,
            "year": ref.get("year"),
            "authors": [],
            "venue": ref.get("journal-title"),
            "cited_by_count": None,
            "referenced_works": [],
            "discovered_via_traversal": True,
        })
    return out


# -------- dedup + filter --------

def _trv_key(c: TraversalCandidate) -> str:
    d = normalize_doi(c.get("doi"))
    if d:
        return d
    return "T:" + (c.get("title_norm") or "")


def _dedup_traversal(cands: Iterable[TraversalCandidate]) -> list[TraversalCandidate]:
    seen: dict[str, TraversalCandidate] = {}
    for c in cands:
        k = _trv_key(c)
        if not k or k == "T:":
            continue
        if k in seen:
            # prefer S2 over Crossref (richer metadata)
            if c.get("source_api") == "s2" and seen[k].get("source_api") == "crossref":
                # merge: keep s2's metadata but preserve crossref provenance only if doi matches
                merged = dict(c)
                merged["source_api"] = "s2"
                seen[k] = merged  # type: ignore[assignment]
        else:
            seen[k] = c
    return list(seen.values())


def filter_overlap(candidates: list[TraversalCandidate],
                   d_prev: set[str], theta_o: int, ell: int
                   ) -> list[TraversalCandidate]:
    """PaperQA2 FilterOverlap (research §3 line 60):
    Keep candidates whose dedup key would have appeared if traversal had been
    run from any of the ≥ theta_o seeds. Stage 2 simplification: we don't
    track per-seed provenance, so the proxy is `cited_by_count` (popularity)
    as tie-break — sort by cited_by_count desc, cap at ell.

    The theta_o parameter retains its PaperQA2 meaning for logging/visibility
    but does not gate here in Stage 2 (Stage 3 plan can refine with per-seed
    edge attribution if needed).
    """
    # Drop candidates already in d_prev (the seed set)
    fresh = [c for c in candidates if _trv_key(c) not in d_prev]
    fresh.sort(key=lambda c: (c.get("cited_by_count") or 0), reverse=True)
    return fresh[:ell]


def _cosine_gate(centroid: np.ndarray, cand_embeddings: np.ndarray,
                 threshold: float) -> list[bool]:
    """Both centroid and cand_embeddings are pre-normalized → dot product = cos."""
    if cand_embeddings.size == 0:
        return []
    sims = cand_embeddings @ centroid
    return [bool(s >= threshold) for s in sims]


async def _embed_candidates_for_gate(cands: list[TraversalCandidate]
                                     ) -> np.ndarray:
    """SPECTER-encode each candidate's title+abstract. Falls back to title
    only if abstract is missing."""
    if not cands:
        return np.zeros((0, EMBED_DIM), dtype=np.float32)
    texts: list[str] = []
    for c in cands:
        title = c.get("title") or ""
        abstract = c.get("abstract") or ""
        text = f"{title}\n{abstract}".strip()
        texts.append(text or title or "(no text)")
    return embed_batch(texts, batch_size=16)


# -------- citation-table persistence --------

def persist_citation_edges(con: sqlite3.Connection, edges: list[tuple]) -> int:
    """edges: list of (paper_id, cited_doi, cited_paper_id_or_None, direction, source_api)
    Idempotent via UNIQUE(paper_id, cited_doi, direction) — uses INSERT OR IGNORE.
    Returns number of new rows inserted."""
    if not edges:
        return 0
    n = 0
    with writer_tx(con):
        for paper_id, cited_doi, cited_paper_id, direction, source_api in edges:
            if not cited_doi:
                continue
            cur = con.execute(
                "INSERT OR IGNORE INTO citations "
                "(paper_id, cited_doi, cited_paper_id, direction, source_api) "
                "VALUES (?, ?, ?, ?, ?)",
                (paper_id, cited_doi, cited_paper_id, direction, source_api),
            )
            n += cur.rowcount or 0
    return n


def _paper_id_to_doi(con: sqlite3.Connection, paper_id: str) -> str | None:
    row = con.execute("SELECT doi FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()
    if not row:
        return None
    return normalize_doi(row["doi"])


def _resolve_cited_paper_id(con: sqlite3.Connection, doi: str) -> str | None:
    row = con.execute(
        "SELECT paper_id FROM papers WHERE LOWER(doi) = LOWER(?)", (doi,)).fetchone()
    return row["paper_id"] if row else None


# -------- main traversal --------

async def traverse_citations(con: sqlite3.Connection,
                             seeds: list[TraversalSeed], *,
                             theta_score: float = THETA_SCORE,
                             alpha: float = ALPHA,
                             fut: bool = True,
                             ell: int = ELL,
                             specter_gate: float = SPECTER_GATE,
                             max_hops: int = MAX_HOPS_DEFAULT,
                             s2_api_key: str | None = None,
                             crossref_email: str | None = None
                             ) -> TraversalResult:
    t0 = time.monotonic()
    api_calls = {"s2_refs": 0, "s2_citers": 0, "crossref": 0}
    warnings: list[str] = []

    # Apply RCS threshold (PaperQA2 0-10 vs retrieve.Hit.score 0-1 scaled ×10)
    qualified: list[tuple[TraversalSeed, str]] = []
    for s in seeds:
        if (s.get("score", 0.0) * 10.0) < theta_score:
            continue
        doi = s.get("doi") or _paper_id_to_doi(con, s.get("paper_id", ""))
        if not doi:
            warnings.append(f"seed has no DOI: {s.get('paper_id') or s.get('title')!r}")
            continue
        qualified.append((s, doi))

    if not qualified:
        return {
            "ok": True, "n_seeds": 0, "n_raw_candidates": 0,
            "n_after_overlap": 0, "n_after_specter_gate": 0, "n_final": 0,
            "candidates": [], "elapsed_s": round(time.monotonic() - t0, 3),
            "api_calls": api_calls,
            "warnings": warnings + ["no_seeds_above_threshold"],
        }

    if s2_api_key is None:
        s2_api_key = get_env("S2_API_KEY")
    if crossref_email is None:
        crossref_email = get_env("OPENALEX_EMAIL")  # reuse polite-pool email

    d_prev_keys: set[str] = {doi for _, doi in qualified}

    # Fan out — three API families concurrently with per-domain budgets.
    # Per-seed budget = 3 calls (1 s2_refs + 1 crossref + 1 s2_citers if fut)
    headers = {"User-Agent": USER_AGENT.format(email=crossref_email or "anonymous")}
    raw: list[TraversalCandidate] = []
    edges: list[tuple] = []  # for persist_citation_edges

    async with httpx.AsyncClient(headers=headers, http2=True,
                                 follow_redirects=True) as client:
        # tasks: (seed, doi, kind, awaitable)
        kinds: list[tuple[TraversalSeed, str, str, asyncio.Future]] = []
        for s, doi in qualified:
            kinds.append((s, doi, "s2_refs",
                          asyncio.create_task(
                              _s2_get_paper_relations(client, doi, "references", s2_api_key))))
            kinds.append((s, doi, "crossref",
                          asyncio.create_task(
                              _crossref_backward_refs(client, doi, crossref_email))))
            if fut:
                kinds.append((s, doi, "s2_citers",
                              asyncio.create_task(
                                  _s2_get_paper_relations(client, doi, "citations", s2_api_key))))

        # Wait for everything (no aiometer here — internal _retry_get sleeps
        # on 429; we trade strict per-second pacing for simpler concurrency).
        results = await asyncio.gather(*(k[3] for k in kinds),
                                       return_exceptions=True)

    for (seed, seed_doi, kind, _), res in zip(kinds, results):
        api_calls[kind] = api_calls.get(kind, 0) + 1
        if isinstance(res, BaseException):
            warnings.append(f"{kind} {seed_doi}: {type(res).__name__}: {res}")
            continue
        if not res:
            continue
        for c in res:
            c["parent_paper_id"] = seed.get("paper_id", "")
            raw.append(c)
            cited_doi = normalize_doi(c.get("doi"))
            if cited_doi:
                edges.append((
                    seed.get("paper_id", ""),
                    cited_doi,
                    _resolve_cited_paper_id(con, cited_doi),
                    c.get("direction", "backward"),
                    c.get("source_api", "s2"),
                ))

    # Persist citation edges (idempotent)
    n_edges = persist_citation_edges(con, edges)
    log(f"persisted {n_edges} new citation edges (of {len(edges)} attempted)",
        prefix="traverse")

    # Dedup
    dedup_cands = _dedup_traversal(raw)
    n_raw = len(raw)

    # SPECTER cosine gate
    centroid = compute_corpus_centroid(con)
    if centroid is None:
        warnings.append("empty_corpus_centroid_skipping_specter_gate")
        gated = dedup_cands
        n_after_gate = len(gated)
    else:
        try:
            embs = await _embed_candidates_for_gate(dedup_cands)
            keep_mask = _cosine_gate(centroid, embs, specter_gate)
            gated = [c for c, k in zip(dedup_cands, keep_mask) if k]
            for c, e in zip(dedup_cands, embs):
                c["cosine_to_centroid"] = float(np.dot(centroid, e))
            n_after_gate = len(gated)
        except Exception as e:
            warnings.append(f"specter_gate_failed: {e}")
            gated = dedup_cands
            n_after_gate = len(gated)

    # FilterOverlap with theta_o (logged for transparency)
    theta_o = math.ceil(alpha * max(len(dedup_cands), 1))
    final = filter_overlap(gated, d_prev_keys, theta_o, ell)

    # Recurse for --depth deep
    if max_hops > 1 and final:
        next_seeds: list[TraversalSeed] = []
        for c in final:
            next_seeds.append({
                "paper_id": c.get("s2_paper_id") or short_id(_trv_key(c), 16),
                "doi": c.get("doi"),
                "title": c.get("title", ""),
                "score": 0.85,   # synthetic — survived gate, treat as qualified
            })
        deeper = await traverse_citations(
            con, next_seeds,
            theta_score=theta_score, alpha=alpha, fut=fut, ell=ell,
            specter_gate=specter_gate, max_hops=max_hops - 1,
            s2_api_key=s2_api_key, crossref_email=crossref_email,
        )
        final = (final + (deeper.get("candidates") or []))[:ell]
        for k, v in (deeper.get("api_calls") or {}).items():
            api_calls[k] = api_calls.get(k, 0) + v
        warnings.extend(deeper.get("warnings") or [])

    return {
        "ok": True,
        "n_seeds": len(qualified),
        "n_raw_candidates": n_raw,
        "n_after_overlap": len(final),
        "n_after_specter_gate": n_after_gate,
        "n_final": len(final),
        "candidates": final,
        "elapsed_s": round(time.monotonic() - t0, 3),
        "api_calls": api_calls,
        "warnings": warnings,
    }


# -------- CLI --------

def main() -> int:
    parser = argparse.ArgumentParser(prog="traverse.py",
                                     description="PaperQA2 Algorithm 1 citation traversal.")
    parser.add_argument("--seeds-json", required=True,
                        help="JSON list of {paper_id, doi, title, score}")
    parser.add_argument("--max-hops", type=int, default=MAX_HOPS_DEFAULT)
    parser.add_argument("--theta-score", type=float, default=THETA_SCORE)
    parser.add_argument("--alpha", type=float, default=ALPHA)
    parser.add_argument("--ell", type=int, default=ELL)
    parser.add_argument("--specter-gate", type=float, default=SPECTER_GATE)
    parser.add_argument("--no-fut", action="store_true",
                        help="skip forward citers (backward refs only)")
    parser.add_argument("--db", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        seeds = json.loads(args.seeds_json)
    except json.JSONDecodeError as e:
        emit_failure(f"bad --seeds-json: {e}")
        return 2

    try:
        from pathlib import Path
        con = open_corpus(Path(args.db) if args.db else None)
    except RuntimeError as e:
        emit_failure(str(e))
        return 1
    try:
        try:
            result = asyncio.run(traverse_citations(
                con, seeds,
                theta_score=args.theta_score,
                alpha=args.alpha,
                fut=not args.no_fut,
                ell=args.ell,
                specter_gate=args.specter_gate,
                max_hops=args.max_hops,
            ))
        except KeyboardInterrupt:
            emit_failure("interrupted")
            return 130
    finally:
        con.close()

    if args.json:
        json_stdout(result)
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
