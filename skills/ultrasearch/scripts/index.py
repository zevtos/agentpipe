#!/usr/bin/env python3
"""
index.py — SPECTER embedding + sqlite-vec upsert for ultrasearch (Stage 1).

Stage 1 scope (01_stage1.md §3.4):
    - Embedding model: sentence-transformers/allenai-specter (768-dim).
    - Vector store: sqlite-vec 0.1.9 vec0 virtual table.
    - Chunking: paragraph-aware, target ~500 tokens, overlap 50 tokens.
    - Writer pattern: BEGIN IMMEDIATE per the dba review of schema.sql.

Stage 2/3 add: specter2 adapters, multilingual-e5, mlx-embeddings, cross-encoder.

CLI:
    index.py --paper-json '{"candidate": {...}, "full_text": "...", "pdf_path": "..."}'
    index.py --batch < ndjson_of_papers

JSON contract (success): {"ok": true, "paper_id": "...", "n_chunks": N,
                          "embed_seconds": float, "warnings": [...]}
JSON contract (failure): {"ok": false, "paper_id"?, "error": "..."}
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import struct
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, TypedDict

from _common import (
    casefold_title,
    count_tokens,
    data_dir,
    emit_failure,
    iso_now,
    json_stdout,
    log,
    normalize_doi,
    short_id,
    skill_dir,
)


# -------- constants --------

EMBED_MODEL_NAME = "sentence-transformers/allenai-specter"
EMBED_DIM = 768
CHUNK_TARGET_TOKENS = 500
CHUNK_OVERLAP_TOKENS = 50
CHUNK_MAX_CHARS = 4000
DEFAULT_BATCH_SIZE = 32
BUSY_TIMEOUT_MS = 5000


class IndexResult(TypedDict, total=False):
    ok: bool
    paper_id: str
    n_chunks: int
    embed_seconds: float
    warnings: list[str]
    error: str


# -------- module-level caches --------

_EMBEDDER = None    # SentenceTransformer instance
_EMBED_DEVICE: str | None = None


def load_embedder():
    """Cached SentenceTransformer loader. Device: MPS on Apple Silicon,
    CUDA if present, else CPU. Falls back gracefully on import error."""
    global _EMBEDDER, _EMBED_DEVICE
    if _EMBEDDER is not None:
        return _EMBEDDER
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        import torch  # type: ignore
    except Exception as e:
        raise RuntimeError(f"sentence-transformers/torch not importable: {e}")

    device = "cpu"
    if torch.cuda.is_available():
        device = "cuda"
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        device = "mps"
    _EMBED_DEVICE = device
    log(f"loading {EMBED_MODEL_NAME} on {device}", prefix="index")
    _EMBEDDER = SentenceTransformer(EMBED_MODEL_NAME, device=device)
    return _EMBEDDER


def embed_device() -> str | None:
    return _EMBED_DEVICE


# -------- corpus connection --------

def open_corpus(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the corpus.db with WAL, load sqlite-vec, ensure schema."""
    if db_path is None:
        db_path = data_dir() / "corpus.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), isolation_level=None, timeout=5.0)
    con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    con.row_factory = sqlite3.Row
    _load_sqlite_vec(con)
    _ensure_schema(con)
    return con


def _load_sqlite_vec(con: sqlite3.Connection) -> None:
    try:
        import sqlite_vec  # type: ignore
    except ImportError as e:
        raise RuntimeError(f"sqlite-vec not importable: {e}")
    try:
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
    except (sqlite3.OperationalError, AttributeError) as e:
        raise RuntimeError(
            f"sqlite-vec extension load failed: {e}. "
            "System SQLite may be too old (need 3.41+) — reinstall Python "
            "from python.org or via Homebrew."
        )


def _ensure_schema(con: sqlite3.Connection) -> None:
    schema_file = skill_dir() / "data" / "schema.sql"
    if not schema_file.is_file():
        raise RuntimeError(f"schema.sql not found at {schema_file}")
    con.executescript(schema_file.read_text())
    _migrate_schema(con)


# Additive columns on `papers`. Keep in lock-step with schema.sql:
# every entry here MUST be NULLABLE so older writers keep succeeding.
# Order matters only cosmetically (ADD COLUMN appends to the end either way).
# New tables introduced by later stages (e.g. Stage 3 `rcs_scores`) don't
# belong here — `CREATE TABLE IF NOT EXISTS` in schema.sql adds them to
# legacy DBs on every connection bootstrap, no Python migration needed.
_ADDITIVE_PAPER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("retracted_at",        "TEXT"),   # Stage 2
    ("h_index",             "REAL"),   # Stage 2
    ("q_score",             "REAL"),   # Stage 2
    ("abstract_translated", "TEXT"),   # Stage 3
    ("institution",         "TEXT"),   # Stage 3
)
_TARGET_SCHEMA_VERSION = "1.2"


def _migrate_schema(con: sqlite3.Connection) -> None:
    """Probe-and-add migration for `papers` additive columns (target 1.2).

    SQLite has no `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, and
    `CREATE TABLE IF NOT EXISTS` in schema.sql is a no-op once `papers`
    exists, so newly-declared columns never reach pre-existing corpus.db
    files without this helper. Strategy:
      1. Read `PRAGMA table_info(papers)` to learn the current columns.
      2. For each missing additive column, issue `ALTER TABLE ADD COLUMN`
         (nullable — never breaks older writers that omit the field).
      3. Bump `schema_meta.schema_version` to the target iff any ALTER ran
         OR the existing marker is stale.

    New tables (e.g. Stage 3 `rcs_scores`) are added by `executescript()`
    via `CREATE TABLE IF NOT EXISTS` and don't require a code path here.

    Idempotent: safe to call on every connection bootstrap. Concurrency-
    safe: wrapped in `BEGIN IMMEDIATE` (same lock pattern as writer_tx);
    if a second upgrading process races us and wins, the `OperationalError`
    on "duplicate column name" is swallowed per-column.
    """
    # Fast path: probe outside the writer lock first. If nothing is
    # missing AND the version marker is already current, skip the
    # IMMEDIATE transaction entirely so read-only callers don't contend.
    existing = {row[1] for row in con.execute("PRAGMA table_info(papers)")}
    missing = [(name, sqltype) for (name, sqltype) in _ADDITIVE_PAPER_COLUMNS
               if name not in existing]
    current_version_row = con.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    current_version = current_version_row[0] if current_version_row else None
    if not missing and current_version == _TARGET_SCHEMA_VERSION:
        return

    con.execute("BEGIN IMMEDIATE")
    try:
        # Re-probe under the writer lock — another process may have
        # finished its own migration between our probe and BEGIN.
        existing = {row[1] for row in con.execute("PRAGMA table_info(papers)")}
        ran_any = False
        for name, sqltype in _ADDITIVE_PAPER_COLUMNS:
            if name in existing:
                continue
            try:
                con.execute(f"ALTER TABLE papers ADD COLUMN {name} {sqltype}")
                log(f"migrate: added papers.{name} {sqltype}", prefix="index")
                ran_any = True
            except sqlite3.OperationalError as e:
                # Race with a concurrent upgrader: it added the column
                # between our re-probe and our ALTER. Treat as success.
                if "duplicate column name" in str(e).lower():
                    log(f"migrate: papers.{name} already present (race) — skipped",
                        prefix="index")
                    continue
                raise

        current_row = con.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        current = current_row[0] if current_row else None
        if (ran_any or current != _TARGET_SCHEMA_VERSION) and current != _TARGET_SCHEMA_VERSION:
            con.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
                (_TARGET_SCHEMA_VERSION,),
            )
            log(f"migrate: schema_version {current or 'NULL'} -> {_TARGET_SCHEMA_VERSION}",
                prefix="index")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


@contextlib.contextmanager
def writer_tx(con: sqlite3.Connection):
    """Use for EVERY write path. BEGIN IMMEDIATE acquires RESERVED at
    transaction start so concurrent writers serialise cleanly instead of
    SQLITE_BUSY mid-transaction (dba review of schema.sql)."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


# -------- vector serialization --------

def _emb_to_blob(vec) -> bytes:
    """Serialize a 768-float vector to little-endian f32 bytes (sqlite-vec
    vec0 expects this format for INSERT)."""
    if hasattr(vec, "astype"):
        # numpy ndarray
        return vec.astype("<f4").tobytes()
    return struct.pack(f"<{len(vec)}f", *vec)


# -------- paper id --------

def make_paper_id(c: dict[str, Any]) -> str:
    """sha256(normalize_doi(doi) or 'T:'+casefold_title)[:16]. Matches the
    discover._candidate_key contract so dedup is stable."""
    d = normalize_doi(c.get("doi"))
    if d:
        return short_id(d, 16)
    title_norm = c.get("title_norm") or casefold_title(c.get("title") or "")
    return short_id("T:" + title_norm, 16)


# -------- chunking --------

def chunk_text(md: str, *,
               target_tokens: int = CHUNK_TARGET_TOKENS,
               overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
               max_chars: int = CHUNK_MAX_CHARS) -> list[str]:
    """Greedy paragraph-aware splitter: accumulate paragraphs until target
    token count is reached, emit a chunk, start the next with the trailing
    `overlap_tokens` words of the previous (sentence-boundary best-effort).
    Returns [] for empty input."""
    if not md or not md.strip():
        return []
    paragraphs = [p.strip() for p in md.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    for p in paragraphs:
        ptokens = count_tokens(p)
        if buf and (buf_tokens + ptokens > target_tokens or
                    sum(len(x) for x in buf) + len(p) > max_chars):
            chunks.append("\n\n".join(buf))
            # carry overlap: take trailing words from last paragraph
            tail_words = (buf[-1].split() if buf else [])
            overlap_words = max(1, overlap_tokens)
            if len(tail_words) > overlap_words:
                tail = " ".join(tail_words[-overlap_words:])
            else:
                tail = " ".join(tail_words)
            buf = [tail] if tail else []
            buf_tokens = count_tokens(tail) if tail else 0
        buf.append(p)
        buf_tokens += ptokens
    if buf:
        chunks.append("\n\n".join(buf))
    # filter out tiny chunks (< 50 chars are mostly noise)
    return [c for c in chunks if len(c) >= 50]


# -------- embedding --------

def embed_batch(texts: list[str], *, batch_size: int = DEFAULT_BATCH_SIZE):
    """Encode a list of texts. Returns numpy ndarray of shape (N, 768),
    normalized so cosine = dot product. Halves batch_size on torch
    RuntimeError (MPS OOM) and falls back to CPU as a last resort."""
    embedder = load_embedder()
    bs = max(1, batch_size)
    while True:
        try:
            return embedder.encode(
                texts,
                batch_size=bs,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )
        except RuntimeError as e:
            if "out of memory" in str(e).lower() and bs > 1:
                bs //= 2
                log(f"embed: OOM at batch_size={bs*2}, retrying with {bs}",
                    prefix="index")
                continue
            raise


# -------- upserts --------

def upsert_paper(con: sqlite3.Connection, c: dict[str, Any], *,
                 pdf_path: str | None = None,
                 pdf_sha256: str | None = None) -> str:
    """INSERT OR REPLACE INTO papers. Returns paper_id. NOT a transaction
    boundary — caller wraps in writer_tx."""
    pid = make_paper_id(c)
    authors = c.get("authors") or []
    refs = c.get("referenced_works") or []
    title = c.get("title") or "(untitled)"
    title_norm = c.get("title_norm") or casefold_title(title)
    con.execute(
        """
        INSERT INTO papers (
            paper_id, doi, arxiv_id, openalex_id, s2_paper_id,
            title, title_norm, abstract, year, venue, authors_json,
            cited_by_count, referenced_works_json,
            pdf_path, pdf_sha256, discovery_source, first_seen_at, last_indexed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  COALESCE((SELECT first_seen_at FROM papers WHERE paper_id = ?), ?),
                  ?)
        ON CONFLICT(paper_id) DO UPDATE SET
            doi              = COALESCE(papers.doi, excluded.doi),
            arxiv_id         = COALESCE(papers.arxiv_id, excluded.arxiv_id),
            openalex_id      = COALESCE(papers.openalex_id, excluded.openalex_id),
            s2_paper_id      = COALESCE(papers.s2_paper_id, excluded.s2_paper_id),
            abstract         = COALESCE(papers.abstract, excluded.abstract),
            year             = COALESCE(papers.year, excluded.year),
            venue            = COALESCE(papers.venue, excluded.venue),
            authors_json     = CASE WHEN excluded.authors_json != '[]'
                                    THEN excluded.authors_json
                                    ELSE papers.authors_json END,
            cited_by_count   = COALESCE(excluded.cited_by_count, papers.cited_by_count),
            referenced_works_json = CASE WHEN excluded.referenced_works_json != '[]'
                                         THEN excluded.referenced_works_json
                                         ELSE papers.referenced_works_json END,
            pdf_path         = COALESCE(excluded.pdf_path, papers.pdf_path),
            pdf_sha256       = COALESCE(excluded.pdf_sha256, papers.pdf_sha256),
            last_indexed_at  = excluded.last_indexed_at
        """,
        (
            pid,
            normalize_doi(c.get("doi")),
            c.get("arxiv_id"),
            c.get("openalex_id"),
            c.get("s2_paper_id"),
            title,
            title_norm,
            c.get("abstract"),
            c.get("year"),
            c.get("venue"),
            json.dumps(authors, ensure_ascii=False),
            c.get("cited_by_count"),
            json.dumps(refs, ensure_ascii=False),
            pdf_path,
            pdf_sha256,
            c.get("source") or "openalex",
            pid, iso_now(),
            iso_now(),
        ),
    )
    return pid


def upsert_chunks(con: sqlite3.Connection, paper_id: str,
                  chunks: list[str], embeddings,
                  model_name: str = EMBED_MODEL_NAME) -> int:
    """Insert chunks + parallel embeddings. Maintains the
    vec_chunks.rowid == chunks.chunk_id invariant. Idempotent: deletes any
    prior chunks for this paper before re-inserting."""
    if not chunks:
        return 0
    if len(chunks) != len(embeddings):
        raise ValueError(f"chunks/embeddings length mismatch: {len(chunks)} vs {len(embeddings)}")

    # delete prior rows (CASCADE from chunks; vec_chunks does NOT cascade
    # because vec0 has no FK support — manual cleanup required)
    rows = con.execute("SELECT chunk_id FROM chunks WHERE paper_id = ?",
                       (paper_id,)).fetchall()
    if rows:
        ids = [r[0] for r in rows]
        # vec_chunks rowid == chunks.chunk_id
        placeholders = ",".join("?" * len(ids))
        con.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({placeholders})", ids)
        con.execute("DELETE FROM chunks WHERE paper_id = ?", (paper_id,))

    n = 0
    for seq, (text, emb) in enumerate(zip(chunks, embeddings)):
        cur = con.execute(
            "INSERT INTO chunks (paper_id, seq, text, n_tokens, model_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (paper_id, seq, text, count_tokens(text), model_name),
        )
        chunk_id = cur.lastrowid
        con.execute(
            "INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
            (chunk_id, _emb_to_blob(emb)),
        )
        n += 1
    return n


def index_paper(con: sqlite3.Connection, candidate: dict[str, Any],
                full_text: str, *,
                pdf_path: str | None = None,
                pdf_sha256: str | None = None) -> IndexResult:
    """Top-level: upsert paper, chunk text, embed, upsert chunks. All
    writes are in a single writer_tx so a failure rolls back cleanly."""
    warnings: list[str] = []
    chunks = chunk_text(full_text)
    if not chunks and (candidate.get("abstract") or "").strip():
        # fallback to abstract as a single chunk
        chunks = [candidate["abstract"]]
        warnings.append("indexed_abstract_only")
    if not chunks:
        # still upsert the paper row (so candidate is known) but no chunks
        with writer_tx(con):
            pid = upsert_paper(con, candidate, pdf_path=pdf_path, pdf_sha256=pdf_sha256)
        return {"ok": True, "paper_id": pid, "n_chunks": 0,
                "embed_seconds": 0.0,
                "warnings": warnings + ["no_indexable_text"], "error": ""}

    t0 = time.monotonic()
    try:
        embeddings = embed_batch(chunks)
    except Exception as e:
        return {"ok": False, "paper_id": make_paper_id(candidate),
                "n_chunks": 0, "embed_seconds": 0.0, "warnings": warnings,
                "error": f"embed_failed: {type(e).__name__}: {e}"}
    embed_seconds = round(time.monotonic() - t0, 3)

    with writer_tx(con):
        pid = upsert_paper(con, candidate, pdf_path=pdf_path, pdf_sha256=pdf_sha256)
        n = upsert_chunks(con, pid, chunks, embeddings)

    return {"ok": True, "paper_id": pid, "n_chunks": n,
            "embed_seconds": embed_seconds,
            "warnings": warnings, "error": ""}


# -------- CLI --------

def _handle_record(con: sqlite3.Connection, rec: dict[str, Any]) -> IndexResult:
    cand = rec.get("candidate")
    if not isinstance(cand, dict):
        return {"ok": False, "paper_id": "", "n_chunks": 0,
                "embed_seconds": 0.0, "warnings": [],
                "error": "missing 'candidate' in input record"}
    full_text = rec.get("full_text") or ""
    return index_paper(
        con, cand, full_text,
        pdf_path=rec.get("pdf_path"),
        pdf_sha256=rec.get("pdf_sha256"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="index.py",
                                     description="Embed and store papers in corpus.db.")
    parser.add_argument("--paper-json", default=None,
                        help='single record JSON: {"candidate":{...},"full_text":"..."}')
    parser.add_argument("--batch", action="store_true",
                        help="read NDJSON records from stdin")
    parser.add_argument("--db", default=None,
                        help="override corpus.db path")
    args = parser.parse_args()

    if not args.batch and not args.paper_json:
        emit_failure("provide --paper-json or --batch")
        return 2

    try:
        con = open_corpus(Path(args.db) if args.db else None)
    except RuntimeError as e:
        emit_failure(str(e))
        return 1

    try:
        if args.batch:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    json_stdout({"ok": False, "error": f"bad_input: {e}"})
                    continue
                json_stdout(_handle_record(con, rec))
            return 0
        try:
            rec = json.loads(args.paper_json)
        except json.JSONDecodeError as e:
            emit_failure(f"bad paper-json: {e}")
            return 2
        result = _handle_record(con, rec)
        json_stdout(result)
        return 0 if result.get("ok") else 1
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
