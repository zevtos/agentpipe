#!/usr/bin/env python3
"""
query_kb.py — citation-first search over a doc2kb knowledge base.

This is the retrieval half of the doc2kb "search engine baked into the KB".
`index_kb.py` builds `<kb_dir>/_index.db` (SQLite FTS5 over structure-aware,
heading-path-headed chunks); this script queries it and returns ranked passages
with a citation an agent can drop straight into an answer:

    papers/transformer.pdf › Results › §3.2  (page 7)   [doc-002]

Design constraints (so a SECOND agent / human can run it anywhere):
  * **Pure stdlib.** Only `sqlite3` + the standard library — no venv, no
    third-party deps, no network. `index_kb.py` copies this file verbatim into
    `<kb_dir>/_query.py` so the KB is self-contained and portable: any machine
    with `python3` can search it via `./query.sh "<question>"`.
  * **FTS5 with a fallback.** FTS5 is a compile-time SQLite option. It is
    present on essentially every mainstream CPython build (probed at index time
    and recorded in the db), but if the `chunks_fts` virtual table is absent
    this script transparently falls back to a pure-Python BM25 over the plain
    `chunks` table. Same ranking semantics, just slower.
  * **BM25 sign.** FTS5 `bm25()` returns *more negative = more relevant*; we
    `ORDER BY bm25(...) ASC` and report a flipped (positive, higher-is-better)
    score so the human output reads naturally.

CLI:
    query_kb.py <kb_dir> "<question>" [options]
    query_kb.py <kb_dir> --info

Options:
    -k / --top-k N     number of passages to return (default 8)
    --doc ID           restrict to a single doc-id (e.g. doc-002)
    --type T           restrict to a source_type (pdf, docx, pptx, ipynb, ...)
    --page N           restrict to a single source page number
    --and              require ALL query terms (default: OR + BM25 ranking)
    --raw              pass the query verbatim as an FTS5 MATCH expression
    --show             print the full chunk body, not just a snippet
    --json             machine-readable JSON (one object per hit)
    --no-color         disable ANSI styling in human output

Exit codes: 0 ok (even with zero hits), 2 usage/db error.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Optional

INDEX_DB_NAME = "_index.db"

# Minimal EN+RU stoplist — dropped from OR queries so ranking is driven by the
# content terms, not by "the/of/и/в". Kept deliberately small; BM25's IDF already
# down-weights ubiquitous terms, this just trims obvious noise from the MATCH.
_STOPWORDS = frozenset("""
a an and are as at be by for from has he in is it its of on that the to was were
will with what which who whom whose why how when where can could should would do
does did not no yes or but if then than this these those there here their our your
и в во не на я с со как а то все она так его но да ты к у же вы за бы по только ее
мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни
быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они тут
где есть надо ней для мы тебя их чем была сам чтоб без чего раз тоже себе под будет
""".split())

_TOKEN_RE = re.compile(r"[0-9A-Za-zÀ-ɏЀ-ӿ]+")


def _fold(s: str) -> str:
    """Lowercase + strip combining diacritics (mirrors FTS5 unicode61
    remove_diacritics on the fallback path so both backends tokenize alike)."""
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(_fold(text))


def _content_terms(query: str) -> list[str]:
    """Tokenize a natural-language query into ranked-search terms, dropping
    stopwords. Falls back to the raw token set if everything was a stopword."""
    toks = _tokenize(query)
    terms = [t for t in toks if t not in _STOPWORDS and len(t) > 1]
    return terms or toks


# ---------- db helpers ----------

def _open_db(kb_dir: Path) -> sqlite3.Connection:
    db_path = kb_dir / INDEX_DB_NAME
    if not db_path.is_file():
        raise FileNotFoundError(
            f"no {INDEX_DB_NAME} in {kb_dir} — build it with "
            f"`ensure_env.py index_kb.py {kb_dir}` (doc2kb Phase 5.5)"
        )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
    ).fetchone()
    if not row:
        return False
    # The table can exist in the schema yet fail to load if this interpreter's
    # sqlite lacks the FTS5 module — probe a no-op MATCH to be certain.
    try:
        conn.execute("SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH 'a' LIMIT 0")
        return True
    except sqlite3.OperationalError:
        return False


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.OperationalError:
        return {}


def _filter_sql(doc: Optional[str], stype: Optional[str], page: Optional[int],
                alias: str = "c") -> tuple[str, list[Any]]:
    clauses, params = [], []
    if doc:
        clauses.append(f"{alias}.doc_id = ?")
        params.append(doc)
    if stype:
        clauses.append(f"{alias}.source_type = ?")
        params.append(stype)
    if page is not None:
        clauses.append(f"{alias}.page = ?")
        params.append(page)
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


# ---------- FTS5 query path ----------

def _build_match(query: str, raw: bool, require_all: bool) -> str:
    if raw:
        return query
    terms = _content_terms(query)
    if not terms:
        return '""'
    quoted = [f'"{t}"' for t in terms]
    return (" AND " if require_all else " OR ").join(quoted)


def _search_fts(conn: sqlite3.Connection, query: str, *, top_k: int, doc: Optional[str],
                stype: Optional[str], page: Optional[int], raw: bool,
                require_all: bool) -> list[dict[str, Any]]:
    match = _build_match(query, raw, require_all)
    where_extra, params = _filter_sql(doc, stype, page)
    # bm25(): context column weighted 2x over body so a hit in the heading path /
    # title (the contextual header) outranks an incidental body match. More
    # negative = more relevant, hence ASC.
    sql = f"""
        SELECT c.chunk_id, c.doc_id, c.source_path, c.kb_path, c.source_type,
               c.page, c.heading, c.body,
               bm25(chunks_fts, 2.0, 1.0) AS score,
               snippet(chunks_fts, 1, '«', '»', ' … ', 14) AS snip
        FROM chunks_fts
        JOIN chunks c ON c.chunk_id = chunks_fts.rowid
        WHERE chunks_fts MATCH ?{where_extra}
        ORDER BY score ASC
        LIMIT ?
    """
    rows = conn.execute(sql, [match, *params, top_k]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["score"] = round(-float(d["score"]), 3)  # flip to higher-is-better
        out.append(d)
    return out


# ---------- pure-Python BM25 fallback ----------

def _search_bm25(conn: sqlite3.Connection, query: str, *, top_k: int, doc: Optional[str],
                 stype: Optional[str], page: Optional[int],
                 require_all: bool) -> list[dict[str, Any]]:
    where_extra, params = _filter_sql(doc, stype, page)
    rows = conn.execute(
        f"""SELECT chunk_id, doc_id, source_path, kb_path, source_type, page,
                   heading, context, body
            FROM chunks c WHERE 1=1{where_extra}""",
        params,
    ).fetchall()
    if not rows:
        return []
    terms = _content_terms(query)
    if not terms:
        return []
    qset = set(terms)

    # Tokenize each chunk; context tokens counted twice to mirror the 2x column
    # weight of the FTS path.
    docs_tokens: list[list[str]] = []
    df: dict[str, int] = {}
    for r in rows:
        toks = _tokenize(r["context"]) * 2 + _tokenize(r["body"])
        docs_tokens.append(toks)
        for t in set(toks) & qset:
            df[t] = df.get(t, 0) + 1
    n = len(rows)
    avgdl = sum(len(t) for t in docs_tokens) / n
    k1, b = 1.5, 0.75
    idf = {t: math.log(1 + (n - df_t + 0.5) / (df_t + 0.5)) for t, df_t in df.items()}

    scored = []
    for r, toks in zip(rows, docs_tokens):
        tf: dict[str, int] = {}
        for t in toks:
            if t in qset:
                tf[t] = tf.get(t, 0) + 1
        if require_all and not qset.issubset(tf.keys()):
            continue
        if not tf:
            continue
        dl = len(toks)
        score = 0.0
        for t, f in tf.items():
            score += idf[t] * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        d = dict(r)
        d.pop("context", None)
        d["score"] = round(score, 3)
        d["snip"] = _make_snippet(r["body"], qset)
        scored.append(d)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def _make_snippet(body: str, qset: set[str], width: int = 220) -> str:
    """Pick the body window densest in query terms (fallback snippet)."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    best, best_hits = body[:width], -1
    for p in paras:
        hits = sum(1 for t in _tokenize(p) if t in qset)
        if hits > best_hits:
            best, best_hits = p, hits
    snip = re.sub(r"\s+", " ", best).strip()
    return (snip[:width] + " …") if len(snip) > width else snip


# ---------- rendering ----------

def _cite(hit: dict[str, Any]) -> str:
    bits = [hit.get("source_path") or hit.get("doc_id") or "?"]
    if hit.get("heading"):
        bits.append(str(hit["heading"]))
    cite = " › ".join(bits)
    if hit.get("page") is not None:
        cite += f"  (page {hit['page']})"
    return cite


def _render_human(hits: list[dict[str, Any]], show: bool, color: bool) -> str:
    if not hits:
        return "no matching passages."
    bold = (lambda s: "\033[1m" + s + "\033[0m") if color else (lambda s: s)
    dim = (lambda s: "\033[2m" + s + "\033[0m") if color else (lambda s: s)
    cyan = (lambda s: "\033[36m" + s + "\033[0m") if color else (lambda s: s)
    out: list[str] = []
    for i, h in enumerate(hits, 1):
        tag = "[{0}] score {1}".format(h.get("doc_id"), h.get("score"))
        out.append(bold("#" + str(i)) + "  " + cyan(_cite(h)) + "   " + dim(tag))
        if show:
            for line in (h.get("body") or "").splitlines():
                out.append("    " + line)
        else:
            out.append("    " + (h.get("snip") or ""))
        out.append(dim("    → " + str(h.get("kb_path"))))
        out.append("")
    return "\n".join(out).rstrip()


def _render_json(hits: list[dict[str, Any]], show: bool) -> str:
    payload = []
    for h in hits:
        rec = {
            "doc_id": h.get("doc_id"),
            "source_path": h.get("source_path"),
            "kb_path": h.get("kb_path"),
            "source_type": h.get("source_type"),
            "page": h.get("page"),
            "heading": h.get("heading"),
            "citation": _cite(h),
            "score": h.get("score"),
            "snippet": h.get("snip"),
        }
        if show:
            rec["text"] = h.get("body")
        payload.append(rec)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _print_info(conn: sqlite3.Connection, kb_dir: Path) -> int:
    meta = _meta(conn)
    n_docs = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    backend = "fts5" if _has_fts(conn) else "python-bm25 (FTS5 unavailable)"
    print(json.dumps({
        "kb_dir": str(kb_dir),
        "index": str(kb_dir / INDEX_DB_NAME),
        "backend": backend,
        "documents": n_docs,
        "chunks": n_chunks,
        "indexer": meta.get("indexer"),
        "created_at": meta.get("created_at"),
        "chunk_target_tokens": meta.get("chunk_target_tokens"),
        "corpus_signature": meta.get("corpus_signature"),
    }, ensure_ascii=False, indent=2))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="query_kb",
        description="Citation-first BM25 search over a doc2kb knowledge base.",
    )
    ap.add_argument("kb_dir")
    ap.add_argument("query", nargs="?", default=None)
    ap.add_argument("-k", "--top-k", type=int, default=8)
    ap.add_argument("--doc", default=None)
    ap.add_argument("--type", dest="stype", default=None)
    ap.add_argument("--page", type=int, default=None)
    ap.add_argument("--and", dest="require_all", action="store_true")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--json", dest="as_json", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--info", action="store_true", help="print index stats and exit")
    args = ap.parse_args(argv)

    kb_dir = Path(args.kb_dir).expanduser().resolve()
    try:
        conn = _open_db(kb_dir)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2

    try:
        if args.info:
            return _print_info(conn, kb_dir)
        if not args.query:
            print("usage: query_kb.py <kb_dir> \"<question>\"  (or --info)",
                  file=sys.stderr)
            return 2
        try:
            if _has_fts(conn):
                hits = _search_fts(
                    conn, args.query, top_k=args.top_k, doc=args.doc,
                    stype=args.stype, page=args.page, raw=args.raw,
                    require_all=args.require_all)
            else:
                hits = _search_bm25(
                    conn, args.query, top_k=args.top_k, doc=args.doc,
                    stype=args.stype, page=args.page, require_all=args.require_all)
        except sqlite3.OperationalError as e:
            print(f"query error: {e}", file=sys.stderr)
            return 2

        if args.as_json:
            print(_render_json(hits, args.show))
        else:
            color = sys.stdout.isatty() and not args.no_color
            print(_render_human(hits, args.show, color))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
