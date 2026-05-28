#!/usr/bin/env python3
"""
quality.py — Retraction Watch ingest + DOI join (Stage 2).

Stage 2 scope (research §6 line 97; 02_stage2.md §3.2):
    - git clone --depth=1 of Retraction Watch CSV mirror
    - parse CSV → upsert into retractions table
    - mark_retractions(papers) joins on lowercased DOI, sets papers.retracted_at

CSV column mapping (Crossref Retraction Watch):
    OriginalPaperDOI → retractions.doi (the paper that was retracted)
    RetractionDate   → retractions.retraction_date
    Reason           → retractions.reason
    entire row       → retractions.raw_record_json

CLI:
    quality.py --refresh                # pull repo, ingest CSV
    quality.py --mark                   # join on DOI, set papers.retracted_at
    quality.py --refresh --mark         # both (orchestrator default)
    quality.py --refresh --force        # bypass 24h cache
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence, TypedDict

from _common import (
    RECENT_YEAR_BASELINE,
    cache_dir,
    cosine,
    emit_failure,
    iso_now,
    json_stdout,
    log,
    normalize_doi,
)
from index import open_corpus, writer_tx


RW_REPO_URL = "https://gitlab.com/crossref/retraction-watch-data.git"
RW_REPO_DIR = cache_dir("retraction-watch")
RW_CSV_FILE = RW_REPO_DIR / "retraction_watch.csv"
RW_REFRESH_INTERVAL_S = 86_400   # 24h
GIT_TIMEOUT_S = 120


class RefreshResult(TypedDict, total=False):
    ok: bool
    csv_rows: int
    inserted: int
    updated: int
    skipped: int
    elapsed_s: float
    csv_path: str
    error: str
    warnings: list[str]


class MarkResult(TypedDict, total=False):
    ok: bool
    n_papers_checked: int
    n_marked: int


def _git_available() -> bool:
    return shutil.which("git") is not None


def _is_repo(p: Path) -> bool:
    return (p / ".git").is_dir()


def _clone_or_pull() -> tuple[bool, str]:
    """Clone --depth=1 if missing, git pull --ff-only otherwise.
    Returns (success, reason_or_path)."""
    if not _git_available():
        return False, "git_not_on_path"

    RW_REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
    try:
        if _is_repo(RW_REPO_DIR):
            log("retraction-watch: git pull --ff-only", prefix="quality")
            r = subprocess.run(
                ["git", "-C", str(RW_REPO_DIR), "pull", "--ff-only"],
                capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False,
            )
            if r.returncode != 0:
                # if pull failed (history rewrite, network blip), rm + reclone
                log(f"retraction-watch: git pull failed: {r.stderr.strip()[:200]}",
                    prefix="quality")
                shutil.rmtree(RW_REPO_DIR, ignore_errors=True)
        if not _is_repo(RW_REPO_DIR):
            log(f"retraction-watch: git clone --depth=1 {RW_REPO_URL}", prefix="quality")
            r = subprocess.run(
                ["git", "clone", "--depth=1", RW_REPO_URL, str(RW_REPO_DIR)],
                capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False,
            )
            if r.returncode != 0:
                return False, f"git_clone_failed: {r.stderr.strip()[:300]}"
    except subprocess.TimeoutExpired:
        return False, "git_timeout"
    except OSError as e:
        return False, f"git_os_error: {e}"

    if not RW_CSV_FILE.is_file():
        return False, "csv_missing_after_clone"
    return True, str(RW_CSV_FILE)


def _csv_mtime_age() -> float | None:
    if not RW_CSV_FILE.is_file():
        return None
    try:
        return time.time() - RW_CSV_FILE.stat().st_mtime
    except OSError:
        return None


def _parse_csv_row(row: dict[str, str]) -> tuple[str | None, str | None, str | None, str]:
    """Returns (normalized_doi, retraction_date, reason, raw_json_blob).
    Empty / unparsable DOI → returns None for doi."""
    raw_doi = (row.get("OriginalPaperDOI") or "").strip()
    doi = normalize_doi(raw_doi)
    rd = (row.get("RetractionDate") or "").strip() or None
    reason = (row.get("Reason") or "").strip() or None
    raw = json.dumps(row, ensure_ascii=False)
    return doi, rd, reason, raw


def _upsert_retraction(con, doi: str, retraction_date: str | None,
                       reason: str | None, raw_json: str) -> tuple[bool, bool]:
    """UPSERT with conflict on UNIQUE(doi). Returns (inserted, updated)."""
    cur = con.execute(
        "SELECT retraction_date, reason FROM retractions WHERE doi = ?", (doi,))
    row = cur.fetchone()
    if row is None:
        con.execute(
            "INSERT INTO retractions (doi, retraction_date, reason, raw_record_json) "
            "VALUES (?, ?, ?, ?)",
            (doi, retraction_date, reason, raw_json),
        )
        return True, False
    # update if changed
    if row["retraction_date"] != retraction_date or row["reason"] != reason:
        con.execute(
            "UPDATE retractions SET retraction_date = ?, reason = ?, "
            "raw_record_json = ? WHERE doi = ?",
            (retraction_date, reason, raw_json, doi),
        )
        return False, True
    return False, False


def refresh_retractions(*, force: bool = False) -> RefreshResult:
    """Pull the Retraction Watch CSV, ingest rows into the retractions table."""
    t0 = time.monotonic()
    warnings: list[str] = []

    if not force:
        age = _csv_mtime_age()
        if age is not None and age < RW_REFRESH_INTERVAL_S:
            log(f"retraction CSV {age:.0f}s old (< {RW_REFRESH_INTERVAL_S}); skip "
                f"refresh (use --force)", prefix="quality")
            warnings.append("skipped_refresh_within_cache")

    if "skipped_refresh_within_cache" not in warnings:
        ok, msg = _clone_or_pull()
        if not ok:
            return {"ok": False, "error": msg,
                    "elapsed_s": round(time.monotonic() - t0, 3),
                    "warnings": warnings}

    if not RW_CSV_FILE.is_file():
        return {"ok": False, "error": "csv_missing",
                "elapsed_s": round(time.monotonic() - t0, 3),
                "warnings": warnings}

    inserted = updated = skipped = csv_rows = 0
    con = open_corpus()
    try:
        with writer_tx(con):
            with RW_CSV_FILE.open(encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    csv_rows += 1
                    doi, rd, reason, raw_json = _parse_csv_row(row)
                    if not doi:
                        skipped += 1
                        continue
                    ins, upd = _upsert_retraction(con, doi, rd, reason, raw_json)
                    inserted += int(ins)
                    updated += int(upd)
    finally:
        con.close()

    return {
        "ok": True, "csv_rows": csv_rows,
        "inserted": inserted, "updated": updated, "skipped": skipped,
        "elapsed_s": round(time.monotonic() - t0, 3),
        "csv_path": str(RW_CSV_FILE),
        "warnings": warnings,
    }


def mark_retractions(con, *, paper_ids: list[str] | None = None) -> MarkResult:
    """Set papers.retracted_at on papers whose DOI matches a retraction row."""
    if paper_ids:
        placeholders = ",".join("?" * len(paper_ids))
        scope_clause = f" AND p.paper_id IN ({placeholders})"
        scope_params = tuple(paper_ids)
    else:
        scope_clause = ""
        scope_params = ()

    rows = con.execute(
        f"""
        SELECT p.paper_id, p.doi, r.retraction_date
        FROM papers p
        JOIN retractions r ON LOWER(p.doi) = LOWER(r.doi)
        WHERE p.retracted_at IS NULL
          AND p.doi IS NOT NULL
          {scope_clause}
        """, scope_params,
    ).fetchall()

    n_marked = 0
    if rows:
        with writer_tx(con):
            for r in rows:
                con.execute(
                    "UPDATE papers SET retracted_at = ? WHERE paper_id = ?",
                    (r["retraction_date"] or iso_now(), r["paper_id"]),
                )
                n_marked += 1
    return {"ok": True, "n_papers_checked": len(rows), "n_marked": n_marked}


# ---------- Stage 3: composite Q-score ----------

# Composite quality weights (research §6 line 98):
#   Q = 0.3·log(cit+1) + 0.2·journal_impact + 0.2·h_index_norm + 0.3·recency
Q_WEIGHTS = {"citations": 0.3, "venue": 0.2, "h_index": 0.2, "recency": 0.3}
# RECENT_YEAR_BASELINE imported from _common (single source).


def _recency_decay(year: int | None) -> float:
    """Returns 0..1, with 1.0 for current year, ~0.5 at -5yr, ~0.25 at -10yr."""
    if not year:
        return 0.0
    delta = max(0, RECENT_YEAR_BASELINE - int(year))
    return math.exp(-delta / 7.0)


def _venue_impact(venue: str | None, cited_by_count: int | None) -> float:
    """Proxy for journal impact when we don't have a real IF lookup: log-scale
    on cited_by_count (it correlates). Stage 3 keeps this dumb but cheap;
    real impact factors are paywalled (Clarivate JCR)."""
    if not cited_by_count or cited_by_count <= 0:
        return 0.0
    return min(1.0, math.log10(cited_by_count + 1) / 4.0)   # caps at ~10k citations


def compute_q_score(*, cited_by_count: int | None, year: int | None,
                    venue: str | None, h_index: float | None) -> float:
    """Composite quality score in [0, 1]. Higher = more trust-worthy / impactful."""
    cit_norm = math.log10((cited_by_count or 0) + 1) / 4.0   # 0..1 saturating
    cit_norm = min(cit_norm, 1.0)
    venue_imp = _venue_impact(venue, cited_by_count)
    h = (h_index or 0.0) / 100.0   # h-index of 100 → 1.0
    h = min(h, 1.0)
    rec = _recency_decay(year)
    return (Q_WEIGHTS["citations"] * cit_norm
            + Q_WEIGHTS["venue"] * venue_imp
            + Q_WEIGHTS["h_index"] * h
            + Q_WEIGHTS["recency"] * rec)


def populate_q_scores(con: sqlite3.Connection, *,
                      paper_ids: list[str] | None = None) -> dict[str, Any]:
    """UPDATE papers.q_score using the composite formula. Idempotent."""
    if paper_ids:
        placeholders = ",".join("?" * len(paper_ids))
        rows = con.execute(
            f"SELECT paper_id, cited_by_count, year, venue, h_index "
            f"FROM papers WHERE paper_id IN ({placeholders})",
            paper_ids,
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT paper_id, cited_by_count, year, venue, h_index FROM papers"
        ).fetchall()
    n = 0
    with writer_tx(con):
        for r in rows:
            q = compute_q_score(
                cited_by_count=r["cited_by_count"],
                year=r["year"],
                venue=r["venue"],
                h_index=r["h_index"],
            )
            con.execute("UPDATE papers SET q_score = ? WHERE paper_id = ?",
                        (q, r["paper_id"]))
            n += 1
    return {"ok": True, "n_papers_scored": n}


# ---------- Stage 3: MMR + diversity caps ----------

def mmr(query_vec: Sequence[float],
        cand_vecs: list[Sequence[float]],
        *, lam: float = 0.7, k: int = 10) -> list[int]:
    """Maximal Marginal Relevance (Carbonell & Goldstein 1998).
    lam=1.0 → pure relevance; lam=0.0 → pure diversity.
    Research §6 line 100 says λ=0.7.
    Returns indices into cand_vecs in selection order."""
    if not cand_vecs:
        return []
    n = len(cand_vecs)
    relevance = [cosine(query_vec, v) for v in cand_vecs]
    selected: list[int] = []
    remaining = set(range(n))
    while remaining and len(selected) < k:
        best_i = -1
        best_score = float("-inf")
        for i in remaining:
            rel = relevance[i]
            if selected:
                div = max(cosine(cand_vecs[i], cand_vecs[j]) for j in selected)
            else:
                div = 0.0
            score = lam * rel - (1 - lam) * div
            if score > best_score:
                best_score = score
                best_i = i
        if best_i < 0:
            break
        selected.append(best_i)
        remaining.discard(best_i)
    return selected


def apply_diversity_caps(hits: list[dict], *,
                         max_institution_pct: float = 0.25,
                         max_author_pct: float = 0.15) -> list[dict]:
    """Drop hits that would exceed institution/author share thresholds.
    Conservative: counted on the running selection, not the final n."""
    if not hits:
        return []
    n = len(hits)
    inst_max = max(1, int(n * max_institution_pct))
    auth_max = max(1, int(n * max_author_pct))
    inst_count: dict[str, int] = {}
    auth_count: dict[str, int] = {}
    kept: list[dict] = []
    for h in hits:
        inst = (h.get("institution") or "").strip().lower()
        authors = h.get("authors") or []
        primary_auth = authors[0].strip().lower() if authors else ""
        # institution cap
        if inst and inst_count.get(inst, 0) >= inst_max:
            continue
        # primary-author cap
        if primary_auth and auth_count.get(primary_auth, 0) >= auth_max:
            continue
        if inst:
            inst_count[inst] = inst_count.get(inst, 0) + 1
        if primary_auth:
            auth_count[primary_auth] = auth_count.get(primary_auth, 0) + 1
        kept.append(h)
    return kept


def retraction_info(con, doi: str) -> dict[str, Any] | None:
    """Lookup helper for synthesize.py."""
    d = normalize_doi(doi)
    if not d:
        return None
    cur = con.execute(
        "SELECT retraction_date, reason FROM retractions WHERE doi = ?", (d,))
    row = cur.fetchone()
    if not row:
        return None
    return {"retraction_date": row["retraction_date"], "reason": row["reason"]}


def main() -> int:
    parser = argparse.ArgumentParser(prog="quality.py",
                                     description="Retraction Watch ingest + DOI join.")
    parser.add_argument("--refresh", action="store_true",
                        help="git clone/pull RW repo, ingest CSV")
    parser.add_argument("--mark", action="store_true",
                        help="mark papers.retracted_at for matched DOIs")
    parser.add_argument("--force", action="store_true",
                        help="bypass 24h CSV cache on --refresh")
    args = parser.parse_args()

    if not (args.refresh or args.mark):
        emit_failure("provide --refresh and/or --mark")
        return 2

    out: dict[str, Any] = {}
    if args.refresh:
        out["refresh"] = refresh_retractions(force=args.force)
    if args.mark:
        con = open_corpus()
        try:
            out["mark"] = mark_retractions(con)
        finally:
            con.close()

    json_stdout({"ok": all(v.get("ok") for v in out.values() if isinstance(v, dict)),
                 **out})
    return 0


if __name__ == "__main__":
    sys.exit(main())
