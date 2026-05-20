#!/usr/bin/env python3
"""
parse.py — pymupdf4llm wrapper for ultrasearch (Stage 1).

CLI:
    parse.py <pdf_path>                  # emits one-line JSON on stdout
    parse.py --batch < ndjson_of_paths   # one line in, one line out, per PDF

Output JSON shape (success):
    {"ok": true, "text": "...", "pages": N, "warnings": [...],
     "extractor": "pymupdf4llm", "char_count": int, "tokens_estimated": int,
     "elapsed_s": float}

Output JSON shape (failure):
    {"ok": false, "reason": "...", "warnings": [...]}

Stage 1 scope (per master plan ADR-002 and 01_stage1.md §3.3):
    - pymupdf4llm only — docling fallback is Stage 2.
    - NO embedded image extraction — that is doc2kb's concern.
    - Picture placeholders (==> picture [WxH] intentionally omitted <==) are
      stripped and counted; a warning surfaces if the count is high so the
      orchestrator knows the math/figures are missing from the text.
    - Mangled visual layout detection mirrors doc2kb's logic (see
      skills/doc2kb/scripts/extract_pdf_pymupdf4llm.py for the canonical
      heuristic) — Stage 2 docling fallback will re-parse mangled pages.

pymupdf4llm is AGPL v3 (transitive via pymupdf). Acceptable for an open
skill; commercial reuse requires a license from Artifex — see
references/parsing-troubleshooting.md.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from _common import (
    count_tokens,
    emit_failure,
    json_stdout,
    log,
)


EXTRACTOR_NAME = "pymupdf4llm"
EXTRACTOR_NAME_HYBRID = "pymupdf4llm+docling"
MIN_TEXT_CHARS = 100   # below this, suspect image-only PDF

# Stage 2 docling fallback thresholds (research §1 line 19; 02_stage2.md §3.4)
PIPE_THRESHOLD_PER_PAGE = 8
MATH_MARKER_RE = re.compile(
    r"\$\$|\\begin\{(?:equation|align|matrix|cases|aligned|bmatrix|pmatrix)\}")
DOCLING_MAX_PAGES_PER_DOC = 30      # OOM guard (research §14 line 304)
DOCLING_TIMEOUT_PER_PAGE_S = 30.0
PAGE_SPLIT_RE = re.compile(r"\n-{3,}\n")   # pymupdf4llm joins pages with horizontal rules

PICTURE_PLACEHOLDER_RE = re.compile(
    r"==>\s*picture\s*\[\s*\d+\s*x\s*\d+\s*\]\s*intentionally\s*omitted\s*<=="
)
DROPPED_PICTURES_PER_PAGE_MIN = 2.0   # avg pictures/page → suspicious
DROPPED_PICTURES_ABS_MIN = 5          # OR absolute count

# Mangled-layout heuristic (doc2kb's logic — see _detect_mangled_layout there)
MANGLED_CELL_BR_MIN = 6
MANGLED_CELL_FRAG_MAXLEN = 3
MANGLED_CELL_ORPHAN_RATIO = 0.5
MANGLED_TABLE_RATIO = 0.25
MANGLED_MIN_TABLE_CELLS = 4


def _try_import():
    try:
        import pymupdf4llm  # type: ignore
        return pymupdf4llm
    except Exception as e:
        emit_failure(f"pymupdf4llm not importable: {e}")
        sys.exit(1)


_DOCLING_CONVERTER = None
_DOCLING_ERROR: str | None = None


def _try_import_docling():
    """Lazy-loaded DocumentConverter. Returns (converter, None) or
    (None, error_msg). Cached at module level."""
    global _DOCLING_CONVERTER, _DOCLING_ERROR
    if _DOCLING_CONVERTER is not None:
        return _DOCLING_CONVERTER, None
    if _DOCLING_ERROR is not None:
        return None, _DOCLING_ERROR
    try:
        from docling.document_converter import DocumentConverter  # type: ignore
    except Exception as e:
        _DOCLING_ERROR = f"docling not importable: {e}"
        return None, _DOCLING_ERROR
    try:
        _DOCLING_CONVERTER = DocumentConverter()
        return _DOCLING_CONVERTER, None
    except Exception as e:
        _DOCLING_ERROR = f"docling DocumentConverter init failed: {e}"
        return None, _DOCLING_ERROR


def _split_into_pages(md: str) -> list[str]:
    """pymupdf4llm joins pages with `\\n-----\\n`. Split on that."""
    parts = PAGE_SPLIT_RE.split(md)
    return [p for p in parts if p.strip()]


def _page_needs_docling(page: str) -> bool:
    pipe_count = page.count("|")
    if pipe_count >= PIPE_THRESHOLD_PER_PAGE:
        return True
    if MATH_MARKER_RE.search(page):
        return True
    # local mangled-cell signature (per-page scope of _detect_mangled_layout)
    if _detect_mangled_layout(page):
        return True
    # picture placeholders inside one page → math figures dropped
    if len(PICTURE_PLACEHOLDER_RE.findall(page)) >= 2:
        return True
    return False


def _detect_problem_pages(pages: list[str]) -> list[int]:
    out: list[int] = []
    for i, page in enumerate(pages):
        if _page_needs_docling(page):
            out.append(i)
        if len(out) >= DOCLING_MAX_PAGES_PER_DOC:
            break
    return out


def _docling_parse_pages(pdf_path: Path,
                        page_indices: list[int]) -> dict[int, str]:
    """Re-parse 0-based page indices with docling. Returns
    {page_idx: markdown_for_that_page}. Empty dict on any failure."""
    converter, err = _try_import_docling()
    if converter is None:
        log(f"docling fallback unavailable: {err}", prefix="parse")
        return {}
    try:
        result = converter.convert(str(pdf_path))
    except Exception as e:
        log(f"docling convert failed: {e}", prefix="parse")
        return {}
    out: dict[int, str] = {}
    for p in page_indices:
        try:
            md = result.document.export_to_markdown(page_no=p + 1)
        except TypeError:
            # older docling: no page_no kwarg — re-render full doc once
            try:
                md = result.document.export_to_markdown()
                out[p] = md      # full doc as substitute for this page
                continue
            except Exception as e:
                log(f"docling export failed: {e}", prefix="parse")
                break
        except Exception as e:
            log(f"docling page {p + 1} export failed: {e}", prefix="parse")
            continue
        if md and md.strip():
            out[p] = md
    return out


def _stitch_pages(pymupdf_pages: list[str],
                  docling_pages: dict[int, str]) -> str:
    """Replace listed indices with docling output; rejoin with `\\n-----\\n`."""
    out: list[str] = []
    for i, page in enumerate(pymupdf_pages):
        out.append(docling_pages.get(i, page))
    return "\n\n-----\n\n".join(out)


def _validate_pdf_file(p: Path) -> str | None:
    """Returns a `reason` string if the file is invalid; None if OK."""
    if not p.exists():
        return "pdf_not_found"
    try:
        size = p.stat().st_size
    except OSError as e:
        return f"pdf_stat_failed: {e}"
    if size == 0:
        return "pdf_empty"
    try:
        with p.open("rb") as fh:
            magic = fh.read(4)
    except OSError as e:
        return f"pdf_read_failed: {e}"
    if magic != b"%PDF":
        return "not_a_pdf"
    return None


def _strip_picture_placeholders(body: str) -> tuple[str, int]:
    """Remove '==> picture [WxH] intentionally omitted <==' lines, return
    (cleaned_body, removed_count)."""
    matches = PICTURE_PLACEHOLDER_RE.findall(body)
    if not matches:
        return body, 0
    cleaned = PICTURE_PLACEHOLDER_RE.sub("", body)
    # collapse the blank lines left behind
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned, len(matches)


def _detect_mangled_layout(body: str) -> dict | None:
    """Return stats dict if body contains markdown table cells with the
    signature 'orphan-char fragment + <br>' pattern that indicates
    pymupdf4llm dropped positioned math glyphs into a row. Same algorithm
    as doc2kb/scripts/extract_pdf_pymupdf4llm.py _detect_mangled_layout."""
    rows = [ln for ln in body.split("\n") if ln.startswith("|") and "|" in ln[1:]]
    if len(rows) < 2:
        return None
    suspicious_cells = 0
    data_cells = 0
    for row in rows:
        inner = row.strip()
        if inner.startswith("|"):
            inner = inner[1:]
        if inner.endswith("|"):
            inner = inner[:-1]
        cells = inner.split("|")
        for raw in cells:
            cell = raw.strip()
            if not cell:
                continue
            if set(cell) <= {"-", ":", " "}:
                continue
            data_cells += 1
            br_count = cell.count("<br>")
            if br_count < MANGLED_CELL_BR_MIN:
                continue
            fragments = [f.strip() for f in cell.split("<br>") if f.strip()]
            if not fragments:
                continue
            orphans = sum(1 for f in fragments
                          if len(f) <= MANGLED_CELL_FRAG_MAXLEN)
            if orphans / len(fragments) >= MANGLED_CELL_ORPHAN_RATIO:
                suspicious_cells += 1
    if data_cells < MANGLED_MIN_TABLE_CELLS:
        return None
    ratio = suspicious_cells / data_cells
    if ratio < MANGLED_TABLE_RATIO:
        return None
    return {"data_cells": data_cells, "mangled_cells": suspicious_cells,
            "ratio": round(ratio, 2)}


def parse_pdf(pdf_path: Path, *,
              max_pages: int | None = None,
              enable_docling: bool = True) -> dict:
    """Parse a PDF with pymupdf4llm and (optionally) docling on problem pages.

    Returns one of:
      success: {"ok": True, "text", "pages", "warnings", "extractor",
                "char_count", "tokens_estimated", "elapsed_s",
                "docling_pages": [int], "docling_skipped": bool}
      failure: {"ok": False, "reason", "warnings": []}
    """
    t0 = time.monotonic()
    warnings: list[str] = []

    reason = _validate_pdf_file(pdf_path)
    if reason is not None:
        return {"ok": False, "reason": reason, "warnings": warnings}

    pymupdf4llm = _try_import()
    try:
        if max_pages is not None:
            try:
                import pymupdf  # type: ignore
                with pymupdf.open(str(pdf_path)) as doc:
                    actual_total = doc.page_count
            except Exception as e:
                log(f"pymupdf page-count probe failed: {e}", prefix="parse")
                actual_total = max_pages
            n_pages = min(max_pages, actual_total)
            md = pymupdf4llm.to_markdown(str(pdf_path), pages=list(range(n_pages)))
        else:
            md = pymupdf4llm.to_markdown(str(pdf_path))
            # pymupdf4llm doesn't directly tell us page count when not chunked;
            # use pymupdf directly for an accurate page count.
            try:
                import pymupdf  # type: ignore
                with pymupdf.open(str(pdf_path)) as doc:
                    n_pages = doc.page_count
            except Exception as e:
                log(f"pymupdf page-count fallback: {e}", prefix="parse")
                # fallback: count '[page' anchors if any, else 1
                n_pages = max(md.count("\n-----\n"), 1)
    except Exception as e:
        # encrypted/corrupt/unsupported PDF
        msg = str(e).lower()
        if "encrypted" in msg or "password" in msg:
            return {"ok": False, "reason": "pdf_encrypted", "warnings": warnings}
        return {"ok": False, "reason": f"pymupdf4llm: {type(e).__name__}: {e}",
                "warnings": warnings}

    # strip and count dropped pictures
    md, dropped = _strip_picture_placeholders(md)
    if dropped > 0:
        per_page = dropped / max(n_pages, 1)
        if per_page >= DROPPED_PICTURES_PER_PAGE_MIN or dropped >= DROPPED_PICTURES_ABS_MIN:
            warnings.append(f"dropped_pictures:{dropped}")

    # mangled-layout heuristic
    mangled = _detect_mangled_layout(md)
    if mangled:
        warnings.append(f"mangled_visual_layout:{mangled['ratio']}")

    # tidy whitespace
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()

    # Stage 2: docling fallback on problem pages
    docling_pages_used: list[int] = []
    docling_skipped = False
    if enable_docling and md:
        pages_split = _split_into_pages(md)
        problem_indices = _detect_problem_pages(pages_split)
        if problem_indices:
            log(f"docling fallback triggered on {len(problem_indices)} pages: "
                f"{problem_indices}", prefix="parse")
            try:
                docling_out = _docling_parse_pages(pdf_path, problem_indices)
                if docling_out:
                    md = _stitch_pages(pages_split, docling_out)
                    docling_pages_used = sorted(docling_out.keys())
                    # remove warnings that were specifically about these pages
                    warnings = [w for w in warnings
                                if not w.startswith("mangled_visual_layout")
                                and not w.startswith("dropped_pictures")]
                else:
                    docling_skipped = True
                    warnings.append("docling_fallback_unavailable")
            except Exception as e:
                docling_skipped = True
                warnings.append(f"docling_fallback_failed: {type(e).__name__}")

    if len(md) < MIN_TEXT_CHARS:
        warnings.append("empty_or_image_only_pdf")

    return {
        "ok": True,
        "text": md,
        "pages": n_pages,
        "warnings": warnings,
        "extractor": EXTRACTOR_NAME_HYBRID if docling_pages_used else EXTRACTOR_NAME,
        "docling_pages": docling_pages_used,
        "docling_skipped": docling_skipped,
        "char_count": len(md),
        "tokens_estimated": count_tokens(md),
        "elapsed_s": round(time.monotonic() - t0, 3),
    }


def _run_one(path: Path) -> dict:
    return parse_pdf(path)


def main() -> int:
    parser = argparse.ArgumentParser(prog="parse.py",
                                     description="Parse a PDF to markdown via pymupdf4llm.")
    parser.add_argument("pdf_path", nargs="?", help="Path to PDF; omit with --batch")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="Limit pages parsed (for debug)")
    parser.add_argument("--no-docling", action="store_true",
                        help="disable docling fallback (Stage 1 mode)")
    parser.add_argument("--batch", action="store_true",
                        help="Read NDJSON {'pdf_path': '...'} from stdin, emit NDJSON one per line")
    args = parser.parse_args()

    if args.batch:
        n_ok = 0
        n_fail = 0
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                emit_failure(f"bad_input_line: {e}")
                n_fail += 1
                continue
            pdf_path = rec.get("pdf_path") or rec.get("path")
            if not pdf_path:
                emit_failure("missing pdf_path in input")
                n_fail += 1
                continue
            result = parse_pdf(Path(pdf_path), max_pages=args.max_pages,
                               enable_docling=not args.no_docling)
            # forward candidate_key if caller supplied one (so orchestrator
            # can join parse-result back to its candidate)
            if "candidate_key" in rec:
                result["candidate_key"] = rec["candidate_key"]
            json_stdout(result)
            if result.get("ok"):
                n_ok += 1
            else:
                n_fail += 1
        log(f"batch: ok={n_ok} fail={n_fail}", prefix="parse")
        return 0

    if not args.pdf_path:
        emit_failure("pdf_path required (or use --batch)")
        return 2
    result = parse_pdf(Path(args.pdf_path), max_pages=args.max_pages,
                       enable_docling=not args.no_docling)
    json_stdout(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
