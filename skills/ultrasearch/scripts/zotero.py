#!/usr/bin/env python3
"""
zotero.py — BibTeX writer + optional pyzotero push (Stage 2).

Behaviour (02_stage2.md §3.3):
    - Always writes a `.bib` file next to the report (or to --bib-out PATH).
    - If ZOTERO_API_KEY + ZOTERO_USER_ID are set AND --push is on, also adds
      items to the user's Zotero collection (default name: "ultrasearch").
    - bibtexparser handles entry serialization; pyzotero handles the API.

Env vars:
    ZOTERO_API_KEY    (optional — without it, bib-only export)
    ZOTERO_USER_ID    (required when ZOTERO_API_KEY is set)

CLI:
    zotero.py --paper-ids '["abc...","def..."]' --bib-out /tmp/refs.bib
    zotero.py --paper-ids-file /tmp/ids.txt --bib-out /tmp/refs.bib --push
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, TypedDict

from _common import (
    emit_failure,
    get_env,
    json_stdout,
    log,
    slugify,
)
from index import open_corpus


ZOTERO_COLLECTION_NAME = "ultrasearch"
PYZOTERO_BATCH_SIZE = 50


class ExportResult(TypedDict, total=False):
    ok: bool
    bib_path: str
    n_entries: int
    n_pushed_to_zotero: int
    zotero_collection_key: str
    warnings: list[str]
    elapsed_s: float
    error: str


# ---------- helpers ----------

def _split_authors(authors_json: str | None) -> list[str]:
    if not authors_json:
        return []
    try:
        return json.loads(authors_json)
    except Exception:
        return []


def _name_to_creator(name: str) -> dict[str, str]:
    """Best-effort 'First M. Last' → {firstName, lastName}. Falls back to
    a single 'name' field for organisations / unknown formats."""
    parts = name.strip().split()
    if len(parts) >= 2:
        return {"creatorType": "author",
                "firstName": " ".join(parts[:-1]),
                "lastName": parts[-1]}
    return {"creatorType": "author", "name": name.strip()}


def _bib_id(row: sqlite3.Row) -> str:
    """Stable BibTeX key from row. Format: <lastname><year><slug>_<pid_prefix>."""
    authors = _split_authors(row["authors_json"])
    last = authors[0].split()[-1] if authors else "anon"
    year = row["year"] or "nd"
    title = (row["title"] or "").strip()
    first_word = re.findall(r"[A-Za-z]+", title) or ["paper"]
    return f"{slugify(last, 16)}{year}{slugify(first_word[0], 20)}_{row['paper_id'][:6]}"


def _bib_escape(value: str | None) -> str:
    """Wrap in {…}; escape special chars conservatively. None → empty."""
    if not value:
        return "{}"
    s = str(value).replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return "{" + s + "}"


def _row_to_bibtex(row: sqlite3.Row, retraction: dict | None = None) -> str:
    bid = _bib_id(row)
    authors = _split_authors(row["authors_json"])
    fields: list[tuple[str, str]] = []
    fields.append(("title", _bib_escape(row["title"])))
    if authors:
        fields.append(("author", _bib_escape(" and ".join(authors))))
    if row["year"] is not None:
        fields.append(("year", _bib_escape(str(row["year"]))))
    if row["venue"]:
        fields.append(("journal", _bib_escape(row["venue"])))
    if row["doi"]:
        fields.append(("doi", _bib_escape(row["doi"])))
    if row["arxiv_id"]:
        fields.append(("eprint", _bib_escape(row["arxiv_id"])))
        fields.append(("archivePrefix", "{arXiv}"))
    if retraction:
        note = f"RETRACTED ({retraction.get('retraction_date') or 'unknown date'})"
        if retraction.get("reason"):
            note += f": {retraction['reason']}"
        fields.append(("note", _bib_escape(note)))
    body = ",\n  ".join(f"{k} = {v}" for k, v in fields)
    return f"@article{{{bid},\n  {body}\n}}\n"


def _fetch_rows(con: sqlite3.Connection, paper_ids: list[str]) -> list[sqlite3.Row]:
    if not paper_ids:
        return []
    placeholders = ",".join("?" * len(paper_ids))
    return con.execute(
        f"SELECT p.paper_id, p.doi, p.arxiv_id, p.title, p.year, p.venue, "
        f"       p.authors_json, p.retracted_at, "
        f"       r.retraction_date, r.reason "
        f"FROM papers p "
        f"LEFT JOIN retractions r ON LOWER(p.doi) = LOWER(r.doi) "
        f"WHERE p.paper_id IN ({placeholders})",
        paper_ids,
    ).fetchall()


# ---------- public API ----------

def export_bib(con: sqlite3.Connection, paper_ids: list[str],
               out_path: Path) -> tuple[Path, int]:
    rows = _fetch_rows(con, paper_ids)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for r in rows:
            retraction = None
            if r["retraction_date"] or r["reason"] or r["retracted_at"]:
                retraction = {
                    "retraction_date": r["retraction_date"] or r["retracted_at"],
                    "reason": r["reason"],
                }
            fh.write(_row_to_bibtex(r, retraction))
            fh.write("\n")
    return out_path, len(rows)


def push_to_zotero(con: sqlite3.Connection, paper_ids: list[str], *,
                   api_key: str, user_id: str,
                   collection_name: str = ZOTERO_COLLECTION_NAME
                   ) -> tuple[int, str | None]:
    try:
        from pyzotero import zotero  # type: ignore
    except ImportError:
        log("pyzotero not installed; skipping Zotero push", prefix="zotero")
        return 0, None

    rows = _fetch_rows(con, paper_ids)
    if not rows:
        return 0, None

    zot = zotero.Zotero(user_id, "user", api_key)

    # find or create the collection
    coll_key: str | None = None
    try:
        for c in zot.collections():
            if c["data"]["name"] == collection_name:
                coll_key = c["key"]
                break
        if coll_key is None:
            resp = zot.create_collections([{"name": collection_name}])
            coll_key = list(resp.get("success", {}).values())[0]
    except Exception as e:
        log(f"zotero: collection setup failed: {e}", prefix="zotero")

    items: list[dict[str, Any]] = []
    for r in rows:
        authors = _split_authors(r["authors_json"])
        item: dict[str, Any] = {
            "itemType": "journalArticle",
            "title": r["title"],
            "creators": [_name_to_creator(a) for a in authors],
            "date": str(r["year"]) if r["year"] else "",
            "publicationTitle": r["venue"] or "",
            "DOI": r["doi"] or "",
        }
        if r["arxiv_id"]:
            item["extra"] = f"arXiv:{r['arxiv_id']}"
        if coll_key:
            item["collections"] = [coll_key]
        items.append(item)

    n_pushed = 0
    for i in range(0, len(items), PYZOTERO_BATCH_SIZE):
        batch = items[i : i + PYZOTERO_BATCH_SIZE]
        try:
            resp = zot.create_items(batch)
            n_pushed += len(resp.get("success", {}))
        except Exception as e:
            log(f"zotero: batch {i // PYZOTERO_BATCH_SIZE} failed: {e}", prefix="zotero")
    return n_pushed, coll_key


def export(con: sqlite3.Connection, paper_ids: list[str], *,
           bib_path: Path,
           push_zotero: bool = False) -> ExportResult:
    t0 = time.monotonic()
    warnings: list[str] = []

    try:
        path, n = export_bib(con, paper_ids, bib_path)
    except OSError as e:
        return {"ok": False, "error": f"bib_write_failed: {e}",
                "elapsed_s": round(time.monotonic() - t0, 3)}

    n_pushed = 0
    coll_key: str | None = None
    if push_zotero:
        api_key = get_env("ZOTERO_API_KEY")
        user_id = get_env("ZOTERO_USER_ID")
        if not api_key:
            warnings.append("ZOTERO_API_KEY not set; bib written, push skipped")
        elif not user_id:
            return {"ok": False, "bib_path": str(path), "n_entries": n,
                    "error": "ZOTERO_USER_ID required when ZOTERO_API_KEY set",
                    "elapsed_s": round(time.monotonic() - t0, 3)}
        else:
            n_pushed, coll_key = push_to_zotero(
                con, paper_ids, api_key=api_key, user_id=user_id)

    return {
        "ok": True,
        "bib_path": str(path),
        "n_entries": n,
        "n_pushed_to_zotero": n_pushed,
        "zotero_collection_key": coll_key,
        "warnings": warnings,
        "elapsed_s": round(time.monotonic() - t0, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="zotero.py",
                                     description="Export corpus papers to BibTeX / Zotero.")
    parser.add_argument("--paper-ids", default=None,
                        help='JSON list of paper_ids: \'["abc","def"]\'')
    parser.add_argument("--paper-ids-file", default=None,
                        help="path to newline-separated paper_id list")
    parser.add_argument("--bib-out", required=True,
                        help="path to write the .bib file")
    parser.add_argument("--push", action="store_true",
                        help="also push to Zotero (requires ZOTERO_API_KEY + ZOTERO_USER_ID)")
    parser.add_argument("--db", default=None, help="override corpus.db path")
    args = parser.parse_args()

    if not args.paper_ids and not args.paper_ids_file:
        emit_failure("provide --paper-ids or --paper-ids-file")
        return 2

    if args.paper_ids:
        try:
            ids = json.loads(args.paper_ids)
        except json.JSONDecodeError as e:
            emit_failure(f"bad --paper-ids JSON: {e}")
            return 2
    else:
        try:
            ids = [ln.strip() for ln in open(args.paper_ids_file)
                   if ln.strip()]
        except OSError as e:
            emit_failure(f"cannot read --paper-ids-file: {e}")
            return 2

    try:
        con = open_corpus(Path(args.db) if args.db else None)
    except RuntimeError as e:
        emit_failure(str(e))
        return 1
    try:
        result = export(con, ids, bib_path=Path(args.bib_out),
                        push_zotero=args.push)
    finally:
        con.close()
    json_stdout(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
