#!/usr/bin/env python3
"""
index_kb.py — Phase 4.5: build a queryable retrieval index over a doc2kb KB.

The scout→extract→manifest pipeline turns a corpus into per-document Markdown +
an INDEX.md catalog. That catalog lists *headings*, so a second-session agent
still navigates by reading INDEX.md and running grep — which floods it with
unranked hits on a large corpus. This phase closes that gap: it slices every
`docs/*.md` into structure-aware passages and indexes them with SQLite FTS5/BM25
so the agent (or a human) can run `query_kb.py <kb_dir> "<question>"` and get the
top passages back *with the file and line range they sit in*, instead of
grepping blind.

What it builds under `<kb_dir>`:
  * `_index.db`  — SQLite database: `docs`, `chunks` (verbatim passages with
    their line range in the `.md` file), `files` (size + mtime of every
    `docs/**/*.md`, so the searcher can tell the index is behind the docs) and,
    when this CPython's sqlite has FTS5 compiled in, a contentless `chunks_fts`
    virtual table holding each passage run through `query_kb.analyze` (folded,
    tokenized, Snowball-stemmed — Russian and English), tokenized by FTS5's
    `ascii` tokenizer. Stemming has to happen in Python — FTS5's `porter` stems
    English only and custom tokenizers need SQLite's C API — so the analyzer
    lives in `query_kb.py` (the file that travels with the KB) and both sides
    import the one function. FTS5 presence is probed at build time and recorded
    in `meta.fts`; without it `query_kb.py` analyzes `chunks` on the fly and
    scores them with a pure-Python copy of FTS5's bm25(), so rankings match
    either way.
  * `_query.py`  — a verbatim, pure-stdlib copy of `query_kb.py` so the KB is
    self-contained and portable: any machine with `python3` can search it.
    Where a newer doc2kb is installed, that copy hands the query over to it.
  * `query.sh` / `query.cmd` — launchers that run `_query.py` against this KB.

Chunking (the retrieval-quality lever, all deterministic — no LLM, no summary):
  * A passage never crosses a `[page N]` anchor (PDF/PPTX/ipynb), so it carries
    its source page for citation; docs without anchors chunk as one stream.
  * Within a page, split on Markdown headings and blank-line paragraph
    boundaries, accumulating to a token target (default 400, hard cap 512),
    **overlap-free** (2025-2026 evidence: overlap adds index cost without recall
    gain for structure-aware chunkers). Fenced code blocks are never split.
  * Each chunk is indexed with a **contextual header** — `Doc title › heading
    path › page N` — weighted 2x in BM25. This is the deterministic slice of
    Anthropic's Contextual Retrieval: it injects the doc/section context a
    context-free chunk lacks, so a passage about "results" still matches
    "transformer results" even when the body never repeats the title. The
    heading path carries over page breaks.

Idempotent: keyed on a corpus signature (the bytes of every indexed `.md` +
chunk params + schema version + analyzer id). Re-running on an unchanged corpus
is a no-op unless `--force`; an index from an older schema/analyzer rebuilds.
`query_kb.py` calls `build_index()` itself when it finds the index behind the
docs or the schema, so nobody has to remember to.

CLI:
    index_kb.py <kb_dir> [--target 400] [--cap 512] [--no-keywords] [--force] [-q]

Importable: `build_index(kb_dir, ...)` returns the summary dict (used by
update_kb.py and by query_kb.py's self-refresh).
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
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from _common import (  # noqa: E402
    FENCE_RE,
    FRONTMATTER_RE,
    HEADING_RE,
    PAGE_ANCHOR_RE,
    count_tokens,
    read_frontmatter,
    sanitize_heading,
    tool_version_string,
    utc_now_iso,
)
import query_kb as _qk  # noqa: E402  — analyzer + schema id; shipped into every KB as _query.py

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent
INDEX_DB_NAME = _qk.INDEX_DB_NAME
# 2: Python-stemmed, contentless, ascii-tokenized FTS5
# 3: chunks carry their line range; `files` table (drift detection); docs.title/lang;
#    signature over the .md bytes; heading path survives page breaks
INDEX_SCHEMA_VERSION = _qk.INDEX_SCHEMA
DEFAULT_TARGET_TOKENS = 400
DEFAULT_MAX_TOKENS = 512
LOCK_NAME = ".index.lock"
LOCK_STALE_SECONDS = 900
LEAD_TEXT_TOKENS = 40

_TOKEN_RE = re.compile(r"[0-9A-Za-zÀ-ɏЀ-ӿ]+")
_SENT_RE = re.compile(r"(?<=[.!?。;])\s+|\n")
_RU_TERM_RE = re.compile(r"[а-яё]{3,}")
_EN_TERM_RE = re.compile(r"[a-z]{3,}")

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


class IndexBusy(RuntimeError):
    """Another process holds the build lock of this KB."""


# ---------- chunking ----------

_HEAD_NOISE_RE = re.compile(r"<[^>]+>|[*`]+|(?<!\w)_+|_+(?!\w)")


def _clean_heading(text: str) -> str:
    """Heading text without Markdown emphasis / inline HTML — what a hit's
    citation shows and what the contextual header indexes. `**_`uuid`_ |**`
    costs an agent tokens and says nothing."""
    s = _HEAD_NOISE_RE.sub("", sanitize_heading(text, maxlen=160))
    return re.sub(r"\s+", " ", s).strip()[:120]


def _doc_title(body: str, source_path: str) -> str:
    for line in body.splitlines():
        m = HEADING_RE.match(line)
        if m:
            return _clean_heading(m.group(2))
        if line.strip():
            break
    stem = Path(source_path).stem if source_path else ""
    return sanitize_heading(stem, maxlen=120) or (source_path or "document")


def _split_long(text: str, start_line: int, cap: int) -> list[tuple[str, int, int, int]]:
    """Split an over-long block into ~cap-token pieces at sentence/line
    boundaries, one sentence of overlap across each cut. Pieces are verbatim
    slices of the block (table rows keep their line breaks). Returns
    (text, first_line, last_line, tokens) per piece."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _SENT_RE.finditer(text):
        if text[pos:m.start()].strip():
            spans.append((pos, m.start()))
        pos = m.end()
    if text[pos:].strip():
        spans.append((pos, len(text)))
    if len(spans) <= 1:
        return [(text, start_line, start_line + text.count("\n"), count_tokens(text))]

    out: list[tuple[str, int, int, int]] = []

    def emit(group: list[tuple[int, int, int]]) -> None:
        a, b = group[0][0], group[-1][1]
        piece = text[a:b].strip()
        if piece:
            out.append((piece, start_line + text.count("\n", 0, a),
                        start_line + text.count("\n", 0, b),
                        sum(g[2] for g in group)))

    cur: list[tuple[int, int, int]] = []
    cur_tok = 0
    for a, b in spans:
        pt = count_tokens(text[a:b])
        if cur and cur_tok + pt > cap:
            emit(cur)
            cur = cur[-1:]  # one-sentence overlap
            cur_tok = cur[0][2]
        cur.append((a, b, pt))
        cur_tok += pt
    if cur:
        emit(cur)
    return out


def _scan_blocks(lines: list[str], first_line: int) -> list[tuple]:
    """Body lines → events, each carrying 1-based FILE line numbers:
    ("page", N) | ("head", level, text, raw_line, line) | ("text", text, first, last).
    Paragraphs split on blank lines, a heading is its own block, a fenced code
    block is never split."""
    events: list[tuple] = []
    buf: list[str] = []
    start = first_line
    in_fence = False

    def flush() -> None:
        if buf:
            text = "\n".join(buf).rstrip()
            if text.strip():
                events.append(("text", text, start, start + text.count("\n")))
            buf.clear()

    for i, line in enumerate(lines):
        ln = first_line + i
        if FENCE_RE.match(line):
            if not buf:
                start = ln
            buf.append(line)
            in_fence = not in_fence
            continue
        if in_fence:
            buf.append(line)
            continue
        if not line.strip():
            flush()
            continue
        m = PAGE_ANCHOR_RE.match(line)
        if m:
            flush()
            events.append(("page", int(m.group(1))))
            continue
        m = HEADING_RE.match(line)
        if m:
            flush()
            events.append(("head", len(m.group(1)), m.group(2), line.rstrip(), ln))
            continue
        if not buf:
            start = ln
        buf.append(line)
    flush()
    return events


def chunk_doc(body: str, source_path: str, *, target: int = DEFAULT_TARGET_TOKENS,
              cap: int = DEFAULT_MAX_TOKENS,
              first_line: int = 1) -> tuple[str, list[dict[str, Any]]]:
    """Return (doc_title, chunks). Each chunk: {page, heading, body, tokens,
    line_start, line_end} — the line range is where the passage sits in the
    `.md` file (`first_line` = file line of the body's first line), so a search
    hit can be opened with `Read(offset, limit)` instead of a second grep.

    A chunk never crosses a `[page N]` anchor (the page citation stays exact),
    but the heading path does: a section that runs over several pages keeps its
    heading on every one of them."""
    title = _doc_title(body, source_path)
    cur_headings: dict[int, str] = {}
    chunks: list[dict[str, Any]] = []
    buf: list[tuple[str, int, int, int, bool]] = []   # text, first, last, tokens, is_heading
    buf_tokens = 0
    buf_heading = ""
    page: Optional[int] = None

    def hpath() -> str:
        return " › ".join(cur_headings[lvl] for lvl in sorted(cur_headings)
                          if cur_headings[lvl])

    def flush(keep_trailing_headings: bool = False) -> None:
        nonlocal buf, buf_tokens, buf_heading
        carry: list[tuple[str, int, int, int, bool]] = []
        if keep_trailing_headings:
            # A heading left at the very end of a full chunk belongs to the text
            # that follows it — carry it into the next chunk.
            while buf and buf[-1][4]:
                carry.insert(0, buf.pop())
        if buf:
            text = "\n\n".join(b[0] for b in buf).strip()
            if text:
                chunks.append({"page": page, "heading": buf_heading, "body": text,
                               "tokens": sum(b[3] for b in buf) + len(buf) - 1,
                               "line_start": buf[0][1], "line_end": buf[-1][2]})
        buf = carry
        buf_tokens = sum(b[3] for b in carry)
        buf_heading = hpath() if carry else ""

    for ev in _scan_blocks(body.split("\n"), first_line):
        kind = ev[0]
        if kind == "page":
            flush()
            page = ev[1]
            continue
        if kind == "head":
            _, level, htext, raw, ln = ev
            if buf and buf_tokens >= target * 0.5:
                flush()
            for lvl in [l for l in cur_headings if l >= level]:
                del cur_headings[lvl]
            cur_headings[level] = _clean_heading(htext)
            # The chunk is labelled with the heading its text sits under. Text
            # before the chunk's first heading counts only when there is enough
            # of it — a running page header or a stray line does not.
            if sum(b[3] for b in buf if not b[4]) < LEAD_TEXT_TOKENS:
                buf_heading = hpath()
            tok = count_tokens(raw)
            buf.append((raw, ln, ln, tok, True))
            buf_tokens += tok
            continue
        _, text, first, last = ev
        btok = count_tokens(text)
        if btok > cap:
            flush(keep_trailing_headings=True)
            lead = buf                      # headings that introduce this block
            buf, buf_tokens, buf_heading = [], 0, ""
            for n, (sub, a, b, t) in enumerate(_split_long(text, first, cap)):
                if n == 0 and lead:
                    sub = "\n\n".join([x[0] for x in lead] + [sub])
                    a = lead[0][1]
                    t += sum(x[3] for x in lead)
                chunks.append({"page": page, "heading": hpath(), "body": sub,
                               "tokens": t, "line_start": a, "line_end": b})
            continue
        if buf and buf_tokens + btok > cap:
            flush(keep_trailing_headings=True)
        if not buf:
            buf_heading = hpath()
        buf.append((text, first, last, btok, False))
        buf_tokens += btok

    flush()
    return title, chunks


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
    """TF-IDF over STEMS, shown as each stem's most frequent surface form — so
    "кейс, кейса, кейсы, кейсов" is one keyword, not four."""
    import math
    n = len(per_doc_tf)
    grouped: list[dict[str, list]] = []          # stem -> [freq, best_form, best_freq]
    df: dict[str, int] = {}
    for tf in per_doc_tf:
        g: dict[str, list] = {}
        for term, freq in tf.items():
            if len(term) <= 2 or term.isdigit() or term in _STOPWORDS:
                continue
            key = " ".join(_qk.analyze(term)) or term
            rec = g.get(key)
            if rec is None:
                g[key] = [freq, term, freq]
            else:
                rec[0] += freq
                if (freq, term) > (rec[2], rec[1]):
                    rec[1], rec[2] = term, freq
        for key in g:
            df[key] = df.get(key, 0) + 1
        grouped.append(g)
    out: list[list[str]] = []
    for g in grouped:
        scored = [(rec[1], rec[0] * math.log((n + 1) / (df[key] + 0.5)))
                  for key, rec in g.items()]
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        out.append([t for t, _ in scored[:top_n]])
    return out


def _tf(text: str) -> dict[str, int]:
    tf: dict[str, int] = {}
    for tok in _TOKEN_RE.findall(text.lower()):
        tf[tok] = tf.get(tok, 0) + 1
    return tf


def _doc_lang(tf: dict[str, int]) -> Optional[str]:
    """`ru` / `en` / `mixed` by the share of Cyrillic vs Latin words — lets the
    searcher tell an agent that a Russian query cannot reach the English half
    of a corpus (lexical search does not cross languages)."""
    ru = sum(f for t, f in tf.items() if _RU_TERM_RE.fullmatch(t))
    en = sum(f for t, f in tf.items() if _EN_TERM_RE.fullmatch(t))
    if ru + en < 20:
        return None
    share = ru / (ru + en)
    return "ru" if share >= 0.75 else "en" if share <= 0.25 else "mixed"


# ---------- index build ----------

def _load_md(path: Path) -> tuple[str, int, str]:
    """(body, file line of the body's first line, sha1 of the file bytes)."""
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return text, 1, hashlib.sha1(raw).hexdigest()
    return text[m.end():], text.count("\n", 0, m.end()) + 1, hashlib.sha1(raw).hexdigest()


def _collect_docs(kb_dir: Path) -> list[dict[str, Any]]:
    """One record per indexable Markdown file. Flat docs yield one record each;
    a sharded doc (`docs/<id>-<slug>/`) yields one record PER PART, all sharing
    the parent `doc_id` but each carrying its own `kb_path` so a search hit
    cites the small part file. The parent `_index.md` is a TOC, not content —
    skipped so the synthetic table never pollutes the passage index.

    A flat note without frontmatter (an agent wrote it and has not run
    `update_kb.sh` yet) is indexed under its file name, so it is searchable at
    once; `update_kb.py` gives it a `doc-NNN` id later."""
    docs_dir = kb_dir / "docs"
    if not docs_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    # Flat docs at the top level. Skip a flat doc that has a sharded sibling
    # folder (a stale re-extract left next to its shard folder) so the same
    # content isn't indexed twice under one doc_id.
    for p in sorted(docs_dir.glob("*.md")):
        sibling = docs_dir / p.stem / "_index.md"
        if sibling.is_file() and read_frontmatter(sibling).get("sharded"):
            continue
        fm = read_frontmatter(p)
        body, first_line, sha = _load_md(p)
        if not body.strip():
            continue
        out.append({
            "doc_id": str(fm.get("id") or p.stem),
            "source_path": fm.get("source") or p.name,
            "kb_path": f"docs/{p.name}",
            "source_type": fm.get("source_type") or ("unknown" if fm.get("id") else "note"),
            "sha256": fm.get("source_sha256") or "",
            "tokens_estimated": fm.get("tokens_estimated") or 0,
            "body": body, "first_line": first_line, "content_sha": sha,
        })
    # Sharded docs: one folder with an _index.md parent + numbered parts.
    for idx in sorted(docs_dir.glob("*/_index.md")):
        pfm = read_frontmatter(idx)
        if not pfm or not pfm.get("id") or not pfm.get("sharded"):
            continue
        for name in pfm.get("parts") or []:
            part = idx.parent / name
            if not part.is_file():
                continue
            part_fm = read_frontmatter(part)
            if not part_fm:
                continue
            body, first_line, sha = _load_md(part)
            out.append({
                "doc_id": pfm.get("id"),                       # shared parent id
                "source_path": pfm.get("source") or "",
                "kb_path": f"docs/{idx.parent.name}/{name}",   # cite the part file
                "source_type": pfm.get("source_type") or "unknown",
                "sha256": pfm.get("source_sha256") or "",
                "tokens_estimated": part_fm.get("tokens_estimated") or 0,
                "body": body, "first_line": first_line, "content_sha": sha,
            })
    return out


def _corpus_signature(docs: list[dict[str, Any]], target: int, cap: int) -> str:
    """Over the BYTES of the indexed files — a hand-edited note or a re-extract
    with the same source changes it (the source sha256 would not)."""
    h = hashlib.sha256()
    h.update(f"schema={INDEX_SCHEMA_VERSION};analyzer={_qk.ANALYZER};"
             f"target={target};cap={cap}\n".encode())
    for d in sorted(docs, key=lambda x: x["kb_path"]):
        h.update(f"{d['kb_path']}|{d['doc_id']}|{d['content_sha']}\n".encode("utf-8"))
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
            n_chunks INTEGER, keywords TEXT, title TEXT, lang TEXT);
        CREATE TABLE chunks(
            chunk_id INTEGER PRIMARY KEY, doc_id TEXT, source_path TEXT,
            kb_path TEXT, source_type TEXT, page INTEGER, heading TEXT,
            ord INTEGER, tokens INTEGER, context TEXT, body TEXT,
            line_start INTEGER, line_end INTEGER);
        CREATE INDEX idx_chunks_doc ON chunks(doc_id);
        CREATE TABLE files(path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER);
    """)
    if fts:
        # Rows are " ".join(analyze(x)): lowercase tokens of [0-9a-z] +
        # non-ASCII letters separated by single spaces. The `ascii` tokenizer
        # splits only on ASCII non-alphanumerics and folds only ASCII case, so
        # it yields exactly those tokens — FTS5 indexes the analyzer's output
        # verbatim. Contentless (content=''): the stemmed text is not needed
        # after indexing (snippets come from `chunks.body`), so the index
        # costs no more space than the old external-content one.
        conn.execute(
            "CREATE VIRTUAL TABLE chunks_fts USING fts5("
            "context, body, content='', tokenize='ascii')")


def _acquire_lock(kb_dir: Path, wait: float) -> Path:
    """One builder per KB. A lock older than LOCK_STALE_SECONDS belongs to a
    dead process and is taken over."""
    lock = kb_dir / LOCK_NAME
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
                    lock.unlink()
                    continue
            except OSError:
                continue
            if time.monotonic() >= deadline:
                raise IndexBusy(f"another process is indexing {kb_dir}")
            time.sleep(0.2)


def _store_files(conn: sqlite3.Connection, kb_dir: Path) -> None:
    conn.execute("DELETE FROM files")
    conn.executemany("INSERT INTO files(path, size, mtime_ns) VALUES(?,?,?)",
                     [(p, s, m) for p, (s, m) in _qk.docs_listing(kb_dir).items()])


def build_index(kb_dir: Path, *, target: int = DEFAULT_TARGET_TOKENS,
                cap: int = DEFAULT_MAX_TOKENS, keywords: bool = True,
                force: bool = False, write_launchers: bool = True,
                wait: float = 120.0) -> dict[str, Any]:
    kb_dir = Path(kb_dir).expanduser().resolve()
    db_path = kb_dir / INDEX_DB_NAME
    lock = _acquire_lock(kb_dir, wait)
    try:
        return _build_locked(kb_dir, db_path, target=target, cap=cap, keywords=keywords,
                             force=force, write_launchers=write_launchers)
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _build_locked(kb_dir: Path, db_path: Path, *, target: int, cap: int, keywords: bool,
                  force: bool, write_launchers: bool) -> dict[str, Any]:
    docs = _collect_docs(kb_dir)
    signature = _corpus_signature(docs, target, cap)

    if not force and _existing_signature(db_path) == signature:
        n_chunks = 0
        try:
            # Same content, but the files may have been touched or copied: record
            # their new size/mtime so the searcher stops asking for a refresh.
            rw = sqlite3.connect(str(db_path))
            try:
                n_chunks = rw.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                _store_files(rw, kb_dir)
                rw.commit()
            finally:
                rw.close()
        except sqlite3.Error:
            pass
        if write_launchers:
            _write_launchers(kb_dir)
        return {"ok": True, "status": "unchanged", "kb_dir": str(kb_dir),
                "index": str(db_path), "documents": len({d["doc_id"] for d in docs}),
                "chunks": n_chunks}

    # Build into a temp db, then atomically replace.
    tmp_path = kb_dir / f".index.build.{os.getpid()}.db"
    if tmp_path.exists():
        tmp_path.unlink()
    conn = sqlite3.connect(str(tmp_path))
    fts = _fts_available(conn)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        _write_schema(conn, fts)

        chunk_id = 0
        total_chunks = 0
        # `docs` keyed by doc_id (a sharded doc contributes N part records that
        # share one doc_id — they collapse to ONE `docs` row here while their
        # chunks all land in `chunks` with their own per-part kb_path). Ordered
        # so the docs table preserves first-seen order.
        doc_rows: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        for d in docs:
            title, chs = chunk_doc(d["body"], d["source_path"], target=target, cap=cap,
                                   first_line=d["first_line"])
            rec = doc_rows.get(d["doc_id"])
            if rec is None:
                # For a sharded doc the per-part kb_path is `docs/<folder>/<part>`;
                # the doc-level row points at the folder's `_index.md` map.
                kbp = d["kb_path"]
                row_title = title
                if kbp.count("/") >= 2:
                    kbp = kbp.rsplit("/", 1)[0] + "/_index.md"
                    row_title = (sanitize_heading(Path(d["source_path"]).stem, maxlen=120)
                                 or title)
                rec = {"source_path": d["source_path"], "kb_path": kbp,
                       "source_type": d["source_type"], "sha256": d["sha256"],
                       "tokens": 0, "n_chunks": 0, "ord": 0, "tf": {}, "title": row_title}
                doc_rows[d["doc_id"]] = rec
            rec["tokens"] += d.get("tokens_estimated") or sum(c["tokens"] for c in chs)
            for ch in chs:
                chunk_id += 1
                ctx = _context_header(title, ch["heading"], ch["page"])
                conn.execute(
                    "INSERT INTO chunks(chunk_id, doc_id, source_path, kb_path, "
                    "source_type, page, heading, ord, tokens, context, body, "
                    "line_start, line_end) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (chunk_id, d["doc_id"], d["source_path"], d["kb_path"],
                     d["source_type"], ch["page"], ch["heading"] or None, rec["ord"],
                     ch["tokens"], ctx, ch["body"], ch["line_start"], ch["line_end"]))
                if fts:
                    conn.execute(
                        "INSERT INTO chunks_fts(rowid, context, body) VALUES(?,?,?)",
                        (chunk_id, " ".join(_qk.analyze(ctx)),
                         " ".join(_qk.analyze(ch["body"]))))
                rec["ord"] += 1
                # Term counts come from the verbatim body only — never the
                # synthetic context header (which would inject "page", the doc
                # title, etc. as spurious terms). They feed keywords and the
                # doc's language tag.
                for term, freq in _tf(ch["body"]).items():
                    rec["tf"][term] = rec["tf"].get(term, 0) + freq
            rec["n_chunks"] += len(chs)
            total_chunks += len(chs)

        doc_ids = list(doc_rows.keys())
        kw_lists = (_keywords([doc_rows[i]["tf"] for i in doc_ids])
                    if keywords else [[] for _ in doc_ids])
        for doc_id, kw in zip(doc_ids, kw_lists):
            rec = doc_rows[doc_id]
            conn.execute(
                "INSERT INTO docs(doc_id, source_path, kb_path, source_type, sha256, "
                "tokens_estimated, n_chunks, keywords, title, lang) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (doc_id, rec["source_path"], rec["kb_path"], rec["source_type"],
                 rec["sha256"], rec["tokens"], rec["n_chunks"], ", ".join(kw),
                 rec["title"], _doc_lang(rec["tf"])))
        _store_files(conn, kb_dir)

        meta = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "indexer": f"doc2kb-index@{tool_version_string()}",
            "engine_rev": str(_qk.ENGINE_REV),
            "created_at": utc_now_iso(),
            "corpus_signature": signature,
            "fts": "1" if fts else "0",
            "analyzer": _qk.ANALYZER,
            "chunk_target_tokens": str(target),
            "chunk_max_tokens": str(cap),
            "documents": str(len(doc_rows)),
            "chunks": str(total_chunks),
        }
        conn.executemany("INSERT INTO meta(key, value) VALUES(?,?)", list(meta.items()))
        conn.commit()
    except BaseException:
        conn.close()
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    conn.close()

    os.replace(tmp_path, db_path)
    if write_launchers:
        _write_launchers(kb_dir)
    return {"ok": True, "status": "built", "kb_dir": str(kb_dir),
            "index": str(db_path), "backend": "fts5" if fts else "python-bm25",
            "documents": len(doc_rows), "chunks": total_chunks}


# ---------- portable launchers ----------

_QUERY_SH = """#!/usr/bin/env bash
# doc2kb KB search — generated by index_kb.py. Pure stdlib, portable.
# Usage: ./query.sh "<question>" ["<another question>" ...] [-k N] [--doc ID] [--brief] [--json]
set -euo pipefail
KB="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$KB/_query.py" "$KB" "$@"
"""

_QUERY_CMD = (
    "@echo off\r\n"
    "REM doc2kb KB search — generated by index_kb.py. Pure stdlib, portable.\r\n"
    'python3 "%~dp0_query.py" "%~dp0." %*\r\n'
)

_AGENTS_MARK = "# Knowledge Base — Agent Instructions"


def _write_if_changed(path: Path, text: str) -> bool:
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    path.write_text(text, encoding="utf-8")
    return True


def _write_launchers(kb_dir: Path) -> None:
    """Drop the portable searcher next to the index: `_query.py` (this skill's
    query_kb.py, verbatim) + `query.sh` / `query.cmd`. A generated `AGENTS.md`
    that is already there is brought up to this skill's template too — it
    documents the searcher's interface, so it ages with it."""
    src = SCRIPTS_DIR / "query_kb.py"
    if src.is_file():
        _write_if_changed(kb_dir / "_query.py", src.read_text(encoding="utf-8"))
    sh = kb_dir / "query.sh"
    _write_if_changed(sh, _QUERY_SH)
    try:
        sh.chmod(0o755)
    except OSError:
        pass
    _write_if_changed(kb_dir / "query.cmd", _QUERY_CMD)
    agents, template = kb_dir / "AGENTS.md", SKILL_DIR / "assets" / "agents_template.md"
    try:
        if (agents.is_file() and template.is_file()
                and agents.read_text(encoding="utf-8").startswith(_AGENTS_MARK)):
            _write_if_changed(agents, template.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        pass


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="index_kb",
        description="Phase 4.5 — build a queryable BM25 retrieval index over a "
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
    try:
        summary = build_index(kb_dir, target=args.target, cap=args.cap,
                              keywords=not args.no_keywords, force=args.force)
    except IndexBusy as e:
        print(json.dumps({"ok": False, "reason": str(e)}))
        return 2
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
