#!/usr/bin/env python3
"""
retrieve.py — 3-stage retrieval (Stage 3).

Pipeline (research §4 lines 82-86):
    Stage A: sqlite-vec MATCH → top-50 by cosine distance
    Stage B: BAAI/bge-reranker-base CrossEncoder → top-10
    Stage C: RCS-LLM scoring per chunk → 0-10 score, keep ≥5
              (cached in rcs_scores table keyed by sha256(query)[:16])

`--profile fast` (or `topk()` direct call) bypasses Stages B+C and returns
the original Stage 1 single-vec result. `--profile full` triggers all three.

CLI:
    retrieve.py --query "<topic>" [--top-k N] [--json] [--profile fast|full]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, TypedDict

from _common import emit_failure, json_stdout, log, sha256_str
from index import (
    EMBED_MODEL_NAME,
    _emb_to_blob,
    embed_batch,
    open_corpus,
    writer_tx,
)


class Hit(TypedDict, total=False):
    chunk_id: int
    paper_id: str
    seq: int
    text: str
    distance: float
    score: float
    doi: str | None
    arxiv_id: str | None
    title: str
    year: int | None
    authors: list[str]
    venue: str | None
    cited_by_count: int | None
    # Stage 3 additions
    rerank_score: float | None      # populated by cross-encoder
    rcs_score: float | None         # populated from rcs_scores cache (0-10)
    rcs_summary: str | None         # contextual summary from RCS scorer
    institution: str | None         # filled in by retrieve_3stage from papers table


RERANKER_MODEL_NAME = "BAAI/bge-reranker-base"
RCS_KEEP_THRESHOLD = 5.0      # PaperQA2 RCS 0-10 scale; keep ≥5 (research §4 line 85)
STAGE_A_TOP_K = 50            # vec MATCH wide net
STAGE_B_TOP_K = 10            # cross-encoder narrows


def _query_hash(query: str) -> str:
    return sha256_str(query)[:16]


def topk(con: sqlite3.Connection, query_text: str, *, k: int = 20,
         model_name: str = EMBED_MODEL_NAME) -> list[Hit]:
    """Embed `query_text` once, run vec MATCH, join to chunks+papers.
    Returns ≤k Hit dicts ordered by ascending distance (cosine ≈ 1 - dist)."""
    if not query_text.strip():
        return []
    q_emb = embed_batch([query_text])[0]
    blob = _emb_to_blob(q_emb)

    # sqlite-vec vec0 KNN syntax: MATCH + k=? must both live in WHERE
    # (no ORDER BY+LIMIT — that raises "A LIMIT or 'k = ?' constraint is
    # required on vec0 knn queries"). We over-fetch then filter by model_name
    # in Python since the vec MATCH only sees rowid/embedding, not metadata.
    sql = """
        SELECT
            c.chunk_id, c.paper_id, c.seq, c.text, v.distance,
            p.doi, p.arxiv_id, p.title, p.year,
            p.authors_json, p.venue, p.cited_by_count
        FROM vec_chunks v
        JOIN chunks c ON c.chunk_id = v.rowid
        JOIN papers p ON p.paper_id = c.paper_id
        WHERE v.embedding MATCH ? AND k = ?
          AND c.model_name = ?
        ORDER BY v.distance
    """
    # Over-fetch 4× to compensate for the model_name post-filter
    rows = con.execute(sql, (blob, max(k * 4, 50), model_name)).fetchall()
    rows = rows[:k]
    out: list[Hit] = []
    for r in rows:
        try:
            authors = json.loads(r["authors_json"] or "[]")
        except Exception:
            authors = []
        dist = r["distance"] or 0.0
        # sqlite-vec returns L2 distance for default vec0 type; SPECTER embeddings
        # are normalized (normalize_embeddings=True in encode), so L2² = 2 - 2·cos.
        # Approximation: cos ≈ 1 - dist²/2.  Keep distance + an approximate score.
        score = max(0.0, 1.0 - dist * dist / 2.0)
        out.append({
            "chunk_id": int(r["chunk_id"]),
            "paper_id": r["paper_id"],
            "seq": int(r["seq"]),
            "text": r["text"],
            "distance": float(dist),
            "score": float(score),
            "doi": r["doi"],
            "arxiv_id": r["arxiv_id"],
            "title": r["title"],
            "year": r["year"],
            "authors": authors,
            "venue": r["venue"],
            "cited_by_count": r["cited_by_count"],
        })
    return out


# ---------- Stage B: cross-encoder rerank ----------

_RERANKER = None


def _load_reranker():
    global _RERANKER
    if _RERANKER is not None:
        return _RERANKER
    try:
        from sentence_transformers import CrossEncoder  # type: ignore
    except ImportError as e:
        raise RuntimeError(f"sentence-transformers CrossEncoder not available: {e}")
    log(f"loading reranker {RERANKER_MODEL_NAME} (~80MB cold)", prefix="retrieve")
    _RERANKER = CrossEncoder(RERANKER_MODEL_NAME)
    return _RERANKER


def rerank_cross_encoder(query: str, hits: list[Hit], *,
                         top_k: int = STAGE_B_TOP_K) -> list[Hit]:
    """Re-score `hits` with a query-chunk CrossEncoder and return top_k."""
    if not hits:
        return []
    try:
        reranker = _load_reranker()
    except RuntimeError as e:
        log(f"reranker unavailable: {e}; skipping Stage B", prefix="retrieve")
        return hits[:top_k]
    pairs = [(query, h["text"]) for h in hits]
    try:
        scores = reranker.predict(pairs, show_progress_bar=False)
    except Exception as e:
        log(f"reranker.predict failed: {e}", prefix="retrieve")
        return hits[:top_k]
    for h, s in zip(hits, scores):
        h["rerank_score"] = float(s)
    hits_sorted = sorted(hits, key=lambda h: h.get("rerank_score", 0.0), reverse=True)
    return hits_sorted[:top_k]


# ---------- Stage C: RCS LLM scoring (cached in rcs_scores) ----------

def lookup_rcs_cache(con: sqlite3.Connection, query: str,
                     chunk_ids: list[int]) -> dict[int, dict]:
    """Return {chunk_id: {score, summary}} for already-scored chunks."""
    if not chunk_ids:
        return {}
    qh = _query_hash(query)
    placeholders = ",".join("?" * len(chunk_ids))
    rows = con.execute(
        f"SELECT chunk_id, score, summary FROM rcs_scores "
        f"WHERE query_hash = ? AND chunk_id IN ({placeholders})",
        (qh, *chunk_ids),
    ).fetchall()
    return {r["chunk_id"]: {"score": r["score"], "summary": r["summary"]}
            for r in rows}


def persist_rcs_scores(con: sqlite3.Connection, query: str,
                       entries: list[tuple[str, int, float, str]]
                       ) -> int:
    """entries: list of (paper_id, chunk_id, score, summary). Idempotent
    via UNIQUE(chunk_id, query_hash)."""
    if not entries:
        return 0
    qh = _query_hash(query)
    n = 0
    with writer_tx(con):
        for paper_id, chunk_id, score, summary in entries:
            cur = con.execute(
                "INSERT OR REPLACE INTO rcs_scores "
                "(paper_id, chunk_id, query_hash, score, summary) "
                "VALUES (?, ?, ?, ?, ?)",
                (paper_id, chunk_id, qh, float(score), summary),
            )
            n += cur.rowcount or 0
    return n


def rcs_filter(con: sqlite3.Connection, query: str, hits: list[Hit], *,
               threshold: float = RCS_KEEP_THRESHOLD) -> list[Hit]:
    """Apply previously-scored RCS values (from rcs_scores table) and keep
    chunks with score ≥ threshold. NOTE: this is the cache-aware reader;
    the **scorer** itself is invoked by ultrasearch.py as a Claude subagent
    (it's a prompt template, not a Python call). When no cached score
    exists for a chunk, that chunk is kept (uncertain → conservative)."""
    if not hits:
        return []
    chunk_ids = [int(h["chunk_id"]) for h in hits]
    cached = lookup_rcs_cache(con, query, chunk_ids)
    out: list[Hit] = []
    for h in hits:
        entry = cached.get(int(h["chunk_id"]))
        if entry is None:
            # no score yet — keep with a placeholder marker
            h["rcs_score"] = None
            h["rcs_summary"] = None
            out.append(h)
            continue
        h["rcs_score"] = entry["score"]
        h["rcs_summary"] = entry["summary"]
        if entry["score"] >= threshold:
            out.append(h)
    return out


# ---------- multi-stage public API ----------

def retrieve_3stage(con: sqlite3.Connection, query: str, *,
                    final_k: int = 20,
                    apply_diversity: bool = True) -> list[Hit]:
    """Full Stage 3 pipeline:
        Stage A: top-50 via sqlite-vec MATCH (oversampled)
        Stage B: cross-encoder rerank → wider than final_k so Stage C can prune
        Stage C: RCS-cached filter ≥5 (uncached chunks pass through)
        Optional: Q-score backfill + MMR-like diversity caps
    """
    # Stage B oversample so final_k > STAGE_B_TOP_K is still reachable
    stage_b_k = max(STAGE_B_TOP_K, final_k)
    stage_a = topk(con, query, k=STAGE_A_TOP_K)
    stage_b = rerank_cross_encoder(query, stage_a, top_k=stage_b_k)
    stage_c = rcs_filter(con, query, stage_b)

    if apply_diversity:
        # Lazy import to avoid cycles — quality.py imports index.py which
        # imports retrieve.py only indirectly.
        try:
            import quality as _quality
            # Backfill q_score on the papers we're about to use (so users see
            # the score in --json output even if mark/refresh wasn't run).
            paper_ids = list({h["paper_id"] for h in stage_c})
            if paper_ids:
                _quality.populate_q_scores(con, paper_ids=paper_ids)
            # Annotate hits with institution from papers table for diversity
            if paper_ids:
                placeholders = ",".join("?" * len(paper_ids))
                inst_rows = con.execute(
                    f"SELECT paper_id, institution FROM papers "
                    f"WHERE paper_id IN ({placeholders})",
                    paper_ids,
                ).fetchall()
                inst_map = {r["paper_id"]: r["institution"] for r in inst_rows}
                for h in stage_c:
                    h["institution"] = inst_map.get(h["paper_id"])
            stage_c = _quality.apply_diversity_caps(stage_c)
        except Exception as e:
            log(f"diversity_caps skipped: {e}", prefix="retrieve")

    return stage_c[:final_k]


def main() -> int:
    parser = argparse.ArgumentParser(prog="retrieve.py",
                                     description="Top-k retrieval from corpus.db.")
    parser.add_argument("--query", required=True, help="natural-language query")
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--profile", choices=("fast", "full"), default="fast",
                        help="fast=Stage A only; full=Stage A+B+C")
    parser.add_argument("--json", action="store_true",
                        help="emit one-line JSON on stdout")
    parser.add_argument("--db", default=None, help="override corpus.db path")
    args = parser.parse_args()

    try:
        con = open_corpus(Path(args.db) if args.db else None)
    except RuntimeError as e:
        emit_failure(str(e))
        return 1
    try:
        if args.profile == "full":
            hits = retrieve_3stage(con, args.query, final_k=args.top_k)
        else:
            hits = topk(con, args.query, k=args.top_k)
    finally:
        con.close()

    payload = {"ok": True, "query": args.query, "n_hits": len(hits),
               "profile": args.profile, "hits": hits}
    if args.json:
        json_stdout(payload)
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
