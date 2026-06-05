#!/usr/bin/env python3
"""
index_kb.py — Phase 5.5: build a queryable retrieval index over a doc2kb KB.

The scout→extract→manifest pipeline turns a corpus into per-document Markdown +
an INDEX.md catalog. That catalog lists *headings*, so a second-session agent
still navigates by reading INDEX.md and running grep — which floods it with
unranked hits on a large corpus. This phase closes that gap: it slices every
`docs/*.md` into structure-aware passages and indexes them with SQLite FTS5/BM25
so the agent (or a human) can run `query_kb.py <kb_dir> "<question>"` and get the
top-k passages back *with citations*, instead of grepping blind.

What it builds under `<kb_dir>`:
  * `_index.db`  — SQLite database: `docs`, `chunks`, and (when this CPython's
    sqlite has FTS5 compiled in) a `chunks_fts` virtual table with the
    `porter unicode61 remove_diacritics 2` tokenizer (Latin stemming + Cyrillic
    + diacritic folding). FTS5 presence is probed at build time and recorded in
    `meta.fts`; `query_kb.py` falls back to a pure-Python BM25 if it is absent.
  * `_query.py`  — a verbatim, pure-stdlib copy of `query_kb.py` so the KB is
    self-contained and portable: any machine with `python3` can search it.
  * `query.sh` / `query.cmd` — launchers that run `_query.py` against this KB.

Chunking (the retrieval-quality lever, all deterministic — no LLM, no summary):
  * Split on `[page N]` anchors first (PDF/PPTX/ipynb), so a passage carries its
    source page for citation; docs without anchors chunk as one stream.
  * Within a page, split on Markdown headings and blank-line paragraph
    boundaries, accumulating to a token target (default 400, hard cap 512),
    **overlap-free** (2025-2026 evidence: overlap adds index cost without recall
    gain for structure-aware chunkers). Fenced code blocks are never split.
  * Each chunk is indexed with a **contextual header** — `Doc title › heading
    path › page N` — weighted 2x in BM25. This is the deterministic slice of
    Anthropic's Contextual Retrieval: it injects the doc/section context a
    context-free chunk lacks, so a passage about "results" still matches
    "transformer results" even when the body never repeats the title.

Idempotent: keyed on a corpus signature (sorted doc-id+sha256 + chunk params).
Re-running on an unchanged corpus is a no-op unless `--force`.

CLI:
    index_kb.py <kb_dir> [--target 400] [--cap 512] [--no-keywords] [--force] [-q]

Importable: `build_index(kb_dir, ...)` returns the summary dict (used by
update_kb.py to keep a live KB's index current).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

from _common import (  # noqa: E402
    PAGE_ANCHOR_RE,
    count_tokens,
    read_body,
    read_frontmatter,
    sanitize_heading,
    split_body_by_page_anchors,
    tool_version_string,
    utc_now_iso,
)

SCRIPTS_DIR = Path(__file__).resolve().parent
INDEX_DB_NAME = "_index.db"
INDEX_SCHEMA_VERSION = "1"
DEFAULT_TARGET_TOKENS = 400
DEFAULT_MAX_TOKENS = 512

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
_TOKEN_RE = re.compile(r"[0-9A-Za-zÀ-ɏЀ-ӿ]+")
_SENT_RE = re.compile(r"(?<=[.!?。;])\s+|\n")

# Small EN+RU stoplist for keyword extraction (NOT for FTS — FTS keeps all
# terms and lets IDF down-weight them). Same spirit as query_kb's list.
_STOPWORDS = frozenset("""
a an and are as at be by for from has he in is it its of on that the to was were
will with what which who this these those there or but if then than not no your our
и в во не на я с со как а то все так его но да ты к у же вы за бы по только ее мне
было вот от меня еще нет о из ему когда даже ну вдруг ли если уже или ни быть был
него до вас опять уж вам ведь там потом себя ничего ей может они тут где есть надо
ней для мы тебя их чем была сам без чего раз тоже себе под будет это для что при
""".split())


# ---------- chunking ----------

def _doc_title(body: str, source_path: str) -> str:
    for line in body.splitlines():
        m = HEADING_RE.match(line)
        if m:
            return sanitize_heading(m.group(2), maxlen=120)
        if line.strip():
            break
    stem = Path(source_path).stem if source_path else ""
    return sanitize_heading(stem, maxlen=120) or (source_path or "document")


def _split_blocks(text: str) -> list[str]:
    """Split text into blocks: paragraphs on blank lines, headings as their own
    block, fenced code kept intact."""
    blocks: list[str] = []
    buf: list[str] = []
    in_fence = False

    def flush() -> None:
        if buf:
            joined = "\n".join(buf).rstrip()
            if joined.strip():
                blocks.append(joined)
            buf.clear()

    for line in text.split("\n"):
        if FENCE_RE.match(line):
            buf.append(line)
            in_fence = not in_fence
            continue
        if in_fence:
            buf.append(line)
            continue
        if not line.strip():
            flush()
            continue
        if HEADING_RE.match(line):
            flush()
            blocks.append(line.rstrip())
            continue
        buf.append(line)
    flush()
    return blocks


def _sentence_split(block: str, cap: int) -> list[str]:
    """Split an over-long block into ~cap-token pieces at sentence/line
    boundaries, with a one-sentence overlap to preserve context across the cut."""
    pieces = [p for p in _SENT_RE.split(block) if p and p.strip()]
    if len(pieces) <= 1:
        return [block]
    out: list[str] = []
    cur: list[str] = []
    cur_tok = 0
    for p in pieces:
        pt = count_tokens(p)
        if cur and cur_tok + pt > cap:
            out.append(" ".join(cur).strip())
            cur = cur[-1:]  # one-sentence overlap
            cur_tok = count_tokens(cur[0]) if cur else 0
        cur.append(p)
        cur_tok += pt
    if cur:
        out.append(" ".join(cur).strip())
    return [o for o in out if o.strip()]


def chunk_doc(body: str, source_path: str, *, target: int = DEFAULT_TARGET_TOKENS,
              cap: int = DEFAULT_MAX_TOKENS) -> tuple[str, list[dict[str, Any]]]:
    """Return (doc_title, chunks). Each chunk: {page, heading, body, tokens}."""
    title = _doc_title(body, source_path)
    preamble, sections = split_body_by_page_anchors(body)

    units: list[tuple[Optional[int], str]] = []
    if sections:
        if preamble.strip():
            units.append((None, preamble))
        for page in sorted(sections):
            sec = PAGE_ANCHOR_RE.sub("", sections[page], count=1)
            units.append((page, sec))
    else:
        units.append((None, body))

    chunks: list[dict[str, Any]] = []
    for page, text in units:
        chunks.extend(_chunk_unit(text, page, target, cap))
    return title, chunks


def _chunk_unit(text: str, page: Optional[int], target: int,
                cap: int) -> list[dict[str, Any]]:
    blocks = _split_blocks(text)
    cur_headings: dict[int, str] = {}
    chunks: list[dict[str, Any]] = []
    buf: list[str] = []
    buf_tokens = 0
    buf_heading = ""

    def hpath() -> str:
        return " › ".join(cur_headings[lvl] for lvl in sorted(cur_headings))

    def flush() -> None:
        nonlocal buf, buf_tokens, buf_heading
        if buf:
            body = "\n\n".join(buf).strip()
            if body:
                chunks.append({"page": page, "heading": buf_heading,
                               "body": body, "tokens": count_tokens(body)})
        buf, buf_tokens, buf_heading = [], 0, ""

    for blk in blocks:
        m = HEADING_RE.match(blk)
        if m:
            level = len(m.group(1))
            htext = sanitize_heading(m.group(2), maxlen=120)
            if buf and buf_tokens >= target * 0.5:
                flush()
            for lvl in [l for l in cur_headings if l >= level]:
                del cur_headings[lvl]
            cur_headings[level] = htext
            if not buf:
                buf_heading = hpath()
            buf.append(blk)
            buf_tokens += count_tokens(blk)
            continue

        btok = count_tokens(blk)
        if btok > cap:
            flush()
            for sub in _sentence_split(blk, cap):
                chunks.append({"page": page, "heading": hpath(), "body": sub,
                               "tokens": count_tokens(sub)})
            continue
        if buf and buf_tokens + btok > cap:
            flush()
        if not buf:
            buf_heading = hpath()
        buf.append(blk)
        buf_tokens += btok

    flush()
    return chunks


def _context_header(title: str, heading: str, page: Optional[int]) -> str:
    bits = [title]
    if heading:
        bits.append(heading)
    ctx = " › ".join(bits)
    if page is not None:
        ctx += f" › page {page}"
    return ctx


# ---------- keyword extraction (cheap TF-IDF, optional) ----------

def _keywords(per_doc_tf: list[dict[str, int]], top_n: int = 8) -> list[list[str]]:
    import math
    n = len(per_doc_tf)
    df: dict[str, int] = {}
    for tf in per_doc_tf:
        for term in tf:
            df[term] = df.get(term, 0) + 1
    out: list[list[str]] = []
    for tf in per_doc_tf:
        scored = [
            (term, freq * math.log((n + 1) / (df[term] + 0.5)))
            for term, freq in tf.items()
            if len(term) > 2 and not term.isdigit() and term not in _STOPWORDS
        ]
        scored.sort(key=lambda kv: kv[1], reverse=True)
        out.append([t for t, _ in scored[:top_n]])
    return out


def _tf(text: str) -> dict[str, int]:
    tf: dict[str, int] = {}
    for tok in _TOKEN_RE.findall(text.lower()):
        tf[tok] = tf.get(tok, 0) + 1
    return tf


# ---------- index build ----------

def _collect_docs(kb_dir: Path) -> list[dict[str, Any]]:
    docs_dir = kb_dir / "docs"
    if not docs_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(docs_dir.glob("*.md")):
        fm = read_frontmatter(p)
        if not fm or not fm.get("id"):
            continue
        out.append({
            "doc_id": fm.get("id"),
            "source_path": fm.get("source") or "",
            "kb_path": f"docs/{p.name}",
            "source_type": fm.get("source_type") or "unknown",
            "sha256": fm.get("source_sha256") or "",
            "tokens_estimated": fm.get("tokens_estimated") or 0,
            "body": read_body(p),
        })
    return out


def _corpus_signature(docs: list[dict[str, Any]], target: int, cap: int) -> str:
    h = hashlib.sha256()
    h.update(f"schema={INDEX_SCHEMA_VERSION};target={target};cap={cap}\n".encode())
    for d in sorted(docs, key=lambda x: str(x["doc_id"])):
        h.update(f"{d['doc_id']}|{d['sha256']}\n".encode("utf-8"))
    return h.hexdigest()


def _existing_signature(db_path: Path) -> Optional[str]:
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='corpus_signature'").fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _fts_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _write_schema(conn: sqlite3.Connection, fts: bool) -> None:
    conn.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE docs(
            doc_id TEXT PRIMARY KEY, source_path TEXT, kb_path TEXT,
            source_type TEXT, sha256 TEXT, tokens_estimated INTEGER,
            n_chunks INTEGER, keywords TEXT);
        CREATE TABLE chunks(
            chunk_id INTEGER PRIMARY KEY, doc_id TEXT, source_path TEXT,
            kb_path TEXT, source_type TEXT, page INTEGER, heading TEXT,
            ord INTEGER, tokens INTEGER, context TEXT, body TEXT);
        CREATE INDEX idx_chunks_doc ON chunks(doc_id);
    """)
    if fts:
        conn.execute(
            "CREATE VIRTUAL TABLE chunks_fts USING fts5("
            "context, body, content='chunks', content_rowid='chunk_id', "
            "tokenize='porter unicode61 remove_diacritics 2')")


def build_index(kb_dir: Path, *, target: int = DEFAULT_TARGET_TOKENS,
                cap: int = DEFAULT_MAX_TOKENS, keywords: bool = True,
                force: bool = False, write_launchers: bool = True) -> dict[str, Any]:
    kb_dir = Path(kb_dir).expanduser().resolve()
    db_path = kb_dir / INDEX_DB_NAME
    docs = _collect_docs(kb_dir)
    signature = _corpus_signature(docs, target, cap)

    if not force and _existing_signature(db_path) == signature:
        n_chunks = 0
        try:
            ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            n_chunks = ro.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            ro.close()
        except sqlite3.Error:
            pass
        if write_launchers:
            _write_launchers(kb_dir)
        return {"ok": True, "status": "unchanged", "kb_dir": str(kb_dir),
                "index": str(db_path), "documents": len(docs), "chunks": n_chunks}

    # Build into a temp db, then atomically replace.
    tmp_path = kb_dir / ".index.build.db"
    if tmp_path.exists():
        tmp_path.unlink()
    conn = sqlite3.connect(str(tmp_path))
    fts = _fts_available(conn)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        _write_schema(conn, fts)

        chunk_id = 0
        per_doc_tf: list[dict[str, int]] = []
        doc_chunk_counts: list[int] = []
        total_chunks = 0
        for d in docs:
            title, chs = chunk_doc(d["body"], d["source_path"], target=target, cap=cap)
            tf: dict[str, int] = {}
            for ordn, ch in enumerate(chs):
                chunk_id += 1
                ctx = _context_header(title, ch["heading"], ch["page"])
                conn.execute(
                    "INSERT INTO chunks(chunk_id, doc_id, source_path, kb_path, "
                    "source_type, page, heading, ord, tokens, context, body) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (chunk_id, d["doc_id"], d["source_path"], d["kb_path"],
                     d["source_type"], ch["page"], ch["heading"] or None, ordn,
                     ch["tokens"], ctx, ch["body"]))
                if keywords:
                    # Keywords come from the verbatim body only — never the
                    # synthetic context header (which would inject "page", the
                    # doc title, etc. as spurious terms).
                    for term, freq in _tf(ch["body"]).items():
                        tf[term] = tf.get(term, 0) + freq
            per_doc_tf.append(tf)
            doc_chunk_counts.append(len(chs))
            total_chunks += len(chs)

        kw_lists = _keywords(per_doc_tf) if keywords else [[] for _ in docs]
        for d, n_ch, kw in zip(docs, doc_chunk_counts, kw_lists):
            conn.execute(
                "INSERT INTO docs(doc_id, source_path, kb_path, source_type, "
                "sha256, tokens_estimated, n_chunks, keywords) VALUES(?,?,?,?,?,?,?,?)",
                (d["doc_id"], d["source_path"], d["kb_path"], d["source_type"],
                 d["sha256"], d["tokens_estimated"], n_ch, ", ".join(kw)))

        if fts:
            conn.execute(
                "INSERT INTO chunks_fts(rowid, context, body) "
                "SELECT chunk_id, context, body FROM chunks")

        meta = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "indexer": f"doc2kb-index@{tool_version_string()}",
            "created_at": utc_now_iso(),
            "corpus_signature": signature,
            "fts": "1" if fts else "0",
            "chunk_target_tokens": str(target),
            "chunk_max_tokens": str(cap),
            "documents": str(len(docs)),
            "chunks": str(total_chunks),
        }
        conn.executemany("INSERT INTO meta(key, value) VALUES(?,?)", list(meta.items()))
        conn.commit()
    finally:
        conn.close()

    os.replace(tmp_path, db_path)
    if write_launchers:
        _write_launchers(kb_dir)
    return {"ok": True, "status": "built", "kb_dir": str(kb_dir),
            "index": str(db_path), "backend": "fts5" if fts else "python-bm25",
            "documents": len(docs), "chunks": total_chunks}


# ---------- portable launchers ----------

_QUERY_SH = """#!/usr/bin/env bash
# doc2kb KB search — generated by index_kb.py. Pure stdlib, portable.
# Usage: ./query.sh "<question>" [--top-k N] [--doc ID] [--type pdf] [--show] [--json]
set -euo pipefail
KB="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$KB/_query.py" "$KB" "$@"
"""

_QUERY_CMD = (
    "@echo off\r\n"
    "REM doc2kb KB search — generated by index_kb.py. Pure stdlib, portable.\r\n"
    'python3 "%~dp0_query.py" "%~dp0." %*\r\n'
)


def _write_launchers(kb_dir: Path) -> None:
    src = SCRIPTS_DIR / "query_kb.py"
    if src.is_file():
        (kb_dir / "_query.py").write_text(src.read_text(encoding="utf-8"),
                                          encoding="utf-8")
    sh = kb_dir / "query.sh"
    sh.write_text(_QUERY_SH, encoding="utf-8")
    try:
        sh.chmod(0o755)
    except OSError:
        pass
    (kb_dir / "query.cmd").write_text(_QUERY_CMD, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="index_kb",
        description="Phase 5.5 — build a queryable BM25 retrieval index over a "
                    "doc2kb KB.")
    ap.add_argument("kb_dir")
    ap.add_argument("--target", type=int, default=DEFAULT_TARGET_TOKENS,
                    help=f"chunk token target (default {DEFAULT_TARGET_TOKENS})")
    ap.add_argument("--cap", type=int, default=DEFAULT_MAX_TOKENS,
                    help=f"chunk token hard cap (default {DEFAULT_MAX_TOKENS})")
    ap.add_argument("--no-keywords", action="store_true",
                    help="skip per-doc distinctive-term extraction")
    ap.add_argument("--force", action="store_true",
                    help="rebuild even if the corpus signature is unchanged")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    kb_dir = Path(args.kb_dir).expanduser().resolve()
    if not (kb_dir / "docs").is_dir():
        print(json.dumps({"ok": False, "reason": f"no docs/ under {kb_dir}"}))
        return 2
    if args.cap < args.target:
        print(json.dumps({"ok": False, "reason": "--cap must be >= --target"}))
        return 2

    t0 = time.monotonic()
    summary = build_index(kb_dir, target=args.target, cap=args.cap,
                          keywords=not args.no_keywords, force=args.force)
    summary["elapsed_seconds"] = round(time.monotonic() - t0, 2)
    print(json.dumps(summary, ensure_ascii=False))
    if not args.quiet:
        backend = summary.get("backend", "?")
        print(f"  index {summary.get('status')}: {summary.get('documents')} docs, "
              f"{summary.get('chunks')} chunks, backend={backend} → "
              f"{summary.get('index')}", file=sys.stderr)
        print(f"  search it: {kb_dir}/query.sh \"<question>\"", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
