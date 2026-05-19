#!/usr/bin/env python3
"""
extract_pdf_pymupdf4llm.py — Phase 4 extractor for native (text-layer) PDFs.

CLI:
    extract_pdf_pymupdf4llm.py <input_pdf> <output_md>
                              [--doc-id doc-NNN]
                              [--source-rel relative/path/in/corpus.pdf]

Effect:
    Writes <output_md> with YAML frontmatter + Markdown body. Page bodies are
    separated by `[page N]` anchors (research §3.4). Stdout receives a single
    JSON line summarizing the result.

Notes:
    - Uses pymupdf4llm.to_markdown(..., page_chunks=True) so we can inject
      page anchors and preserve per-page metadata.
    - Will flag a warning if total body length looks suspiciously small for
      the page count (likely image-only PDF that slipped past scout).
    - Never crashes the agent: extraction failure → emit_failure, exit 1.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Local-only import — _common lives next to this script (loaded via the
# doc2kb.pth that ensure_env.py wrote).
from _common import (  # noqa: E402
    clean_whitespace,
    count_tokens,
    emit_failure,
    emit_success,
    log,
    sanitize_heading,
    sha256_of,
    today_iso,
    tool_version_string,
    validate_source_rel,
    write_md,
)


EXTRACTOR_NAME = "pymupdf4llm"
MIN_CHARS_PER_PAGE = 30  # below this, suspect image-only or extraction failure

# Mangled-layout heuristic tunables. A "mangled" cell is one where
# pymupdf4llm collapsed visual math layout (absolute-positioned subscripts,
# primes, fraction bars, multi-row equation rendering) into a sequence of
# single-character fragments separated by <br>. The bug is unfixable inside
# pymupdf4llm — the PDF doesn't carry the math as text-layer glyphs, it
# carries them as positioned drawing operations — but we can detect the
# pattern after extraction and tell the agent to re-extract via Claude's
# Read-on-PDF path (which renders the page and reads it visually).
MANGLED_CELL_BR_MIN = 6           # min <br> tags inside a cell to be suspicious
MANGLED_CELL_FRAG_MAXLEN = 3      # fragments ≤ this many chars count as "orphan"
MANGLED_CELL_ORPHAN_RATIO = 0.5   # ≥ this fraction of fragments must be orphans
MANGLED_TABLE_RATIO = 0.25        # ≥ this fraction of data cells must be mangled
MANGLED_MIN_TABLE_CELLS = 4       # tables smaller than this can't trip the flag

# Dropped-pictures heuristic. The second failure mode for math-heavy PDFs
# is that pymupdf4llm cannot extract positioned math at all and instead
# emits `==> picture [W x H] intentionally omitted <==` placeholders.
# Documents heavy with these placeholders are usually scientific PDFs where
# the dropped pictures ARE the equations — body text references "уравнение
# (1)", "формула", "матрица" but the formulas themselves are gone. The kb
# document looks mostly intact but is missing the math the agent will be
# asked about.
PICTURE_PLACEHOLDER_RE = re.compile(
    r"==>\s*picture\s*\[\s*\d+\s*x\s*\d+\s*\]\s*intentionally\s*omitted\s*<=="
)
DROPPED_PICTURES_PER_PAGE_MIN = 2.0  # avg pictures/page above this → suspicious
DROPPED_PICTURES_ABS_MIN = 5         # OR: absolute count above this on any doc


def _try_import():
    try:
        import pymupdf4llm  # type: ignore
        import pymupdf  # type: ignore
        return pymupdf4llm, pymupdf
    except Exception as e:
        emit_failure(f"pymupdf4llm not importable: {e}")
        sys.exit(1)


def _detect_mangled_layout(body: str) -> dict | None:
    """Detect when pymupdf4llm has collapsed a visual math layout
    (absolute-positioned subscripts/primes/fraction bars) into orphan
    single-char fragments interleaved with <br> tags inside markdown table
    cells. The signature is unmistakable when present — see test fixture
    `Варианты задания_оценка параметров.pdf` in the doc2kb regression
    corpus, where rows of ODE equations come out as:

        |1| )<br>(<br>2<br>)<br>(<br>... _t_<br>_y_<br>_t_<br>_y_ | ...

    Returns None if the body looks healthy. Returns a stats dict if
    mangling is detected — caller appends a `mangled_visual_layout`
    warning so the orchestrating agent knows to re-extract this PDF by
    reading it directly via the Read tool (claude-pagewise route).
    """
    # Only markdown table rows can carry the pattern. A row starts with `|`
    # and contains another `|` after the first character.
    rows = [ln for ln in body.split("\n") if ln.startswith("|") and "|" in ln[1:]]
    if len(rows) < 2:
        return None
    suspicious_cells = 0
    data_cells = 0
    for row in rows:
        # Strip leading/trailing pipes so split() doesn't emit two empties.
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
            # Header separator rows look like `---`, `:---:`, etc.
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
    return {
        "table_cells": data_cells,
        "mangled_cells": suspicious_cells,
        "ratio": round(ratio, 2),
    }


def _detect_dropped_pictures(body: str, n_pages: int) -> dict | None:
    """Detect PDFs where pymupdf4llm gave up on positioned math and emitted
    placeholders instead — see test fixture `Преобразование математических
    моделей динамической системы.pdf` in the regression corpus, where 16
    equations across 5 pages came out as `==> picture [WxH] intentionally
    omitted <==` markers (3.2 pictures/page). Such documents look fluent
    but are missing the math the second-session agent will be asked about.

    Returns None if the document looks healthy. Returns a stats dict if
    picture-density crosses either the per-page or absolute threshold —
    caller appends a `dropped_pictures` warning suggesting the same
    re-extract-via-Read remediation as `mangled_visual_layout`.
    """
    matches = PICTURE_PLACEHOLDER_RE.findall(body)
    count = len(matches)
    if count == 0:
        return None
    per_page = count / n_pages if n_pages > 0 else float(count)
    if (per_page >= DROPPED_PICTURES_PER_PAGE_MIN
            or count >= DROPPED_PICTURES_ABS_MIN):
        return {
            "count": count,
            "pages": n_pages,
            "per_page": round(per_page, 2),
        }
    return None


def extract(input_pdf: Path) -> tuple[str, dict, list[str]]:
    """Returns (body_markdown, frontmatter_extras, warnings)."""
    pymupdf4llm, pymupdf = _try_import()

    warnings: list[str] = []
    extras: dict = {}

    # Open with pymupdf first — gives us page count cheaply.
    try:
        doc = pymupdf.open(str(input_pdf))
    except Exception as e:
        raise RuntimeError(f"failed to open pdf: {e}") from e
    n_pages = doc.page_count
    doc.close()

    extras["pages"] = n_pages

    # Page-chunked extraction. Each chunk is a dict with 'text', 'metadata',
    # 'tables', 'images', etc. We only use 'text' here.
    try:
        chunks = pymupdf4llm.to_markdown(str(input_pdf), page_chunks=True,
                                         show_progress=False)
    except Exception as e:
        raise RuntimeError(f"to_markdown failed: {e}") from e

    pieces: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        text = (chunk.get("text") or "").strip()
        if not text:
            continue
        pieces.append(f"[page {i}]\n\n{text}")

    body = "\n\n".join(pieces).strip() + "\n"
    body = clean_whitespace(body)

    # Min-length guard (R2 in risk register).
    total_chars = len(body)
    if n_pages > 0 and total_chars < n_pages * MIN_CHARS_PER_PAGE:
        warnings.append(
            f"suspiciously small extraction: {total_chars} chars over {n_pages} pages "
            f"(possible scanned/image-only PDF that bypassed scout)"
        )

    # Mangled visual-math-layout guard. pymupdf4llm cannot represent PDFs
    # that draw math via absolute positioning (subscripts/primes/fraction
    # bars). When this happens, the orchestrating agent needs to re-extract
    # the PDF via Claude's Read tool (which renders the page) — we can't fix
    # it inside the extractor, but we can flag it loudly.
    mangled = _detect_mangled_layout(body)
    if mangled is not None:
        warnings.append(
            "mangled_visual_layout: detected fragmented text in "
            f"{mangled['mangled_cells']}/{mangled['table_cells']} table cells "
            f"(ratio {mangled['ratio']}) suggestive of visual math/equation "
            "layout that pymupdf4llm cannot represent faithfully — re-extract "
            "this PDF by reading the source file directly via the Read tool "
            "(Claude's PDF reading renders the page) and overwrite the body "
            "of this kb document with a manual transcription"
        )

    # Dropped-pictures guard. The complementary failure mode: positioned
    # math survives as `==> picture [WxH] intentionally omitted <==`
    # placeholders instead of mangled cells. Same remediation: re-read the
    # PDF via the Read tool and transcribe manually.
    dropped = _detect_dropped_pictures(body, n_pages)
    if dropped is not None:
        warnings.append(
            f"dropped_pictures: pymupdf4llm omitted {dropped['count']} "
            f"picture(s) over {dropped['pages']} page(s) "
            f"({dropped['per_page']}/page); in scientific/lab PDFs these "
            "placeholders typically hide equations, matrices, or block "
            "diagrams — body text will reference formulas that aren't in "
            "the extraction. Re-extract this PDF by reading the source file "
            "directly via the Read tool and overwrite the body with a "
            "manual transcription of the dropped figures"
        )

    # Top-level headings for the manifest. pymupdf4llm tends to emit
    # primary headings as `##` (no `#`), so include both `#` and `##`. Keep
    # the cap at 10 to avoid bloating the manifest on long documents.
    headings = []
    for line in body.split("\n"):
        stripped = line.lstrip()
        if (stripped.startswith("# ") or stripped.startswith("## ")) and len(stripped) < 200:
            text = stripped.lstrip("#").lstrip()
            sanitized = sanitize_heading(text)
            if sanitized:
                headings.append(sanitized)
            if len(headings) >= 10:
                break
    # Always emit `headings` (possibly empty) for schema consistency with
    # other extractors.
    extras["headings"] = headings

    return body, extras, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract native PDF → Markdown via pymupdf4llm.")
    ap.add_argument("input_pdf")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    args = ap.parse_args()

    in_path = Path(args.input_pdf).expanduser().resolve()
    out_path = Path(args.output_md).expanduser().resolve()
    source_rel = args.source_rel or in_path.name
    try:
        source_rel = validate_source_rel(source_rel)
    except ValueError as e:
        emit_failure(f"invalid --source-rel: {e}")
        return 1

    if not in_path.is_file():
        emit_failure(f"input not found: {in_path}")
        return 1

    try:
        body, extras, warnings = extract(in_path)
    except Exception as e:
        emit_failure(f"extraction failed: {e}", extra={"input": str(in_path)})
        return 1

    fm = {
        "id": args.doc_id,
        "source": source_rel,
        "source_type": "pdf",
        "source_sha256": sha256_of(in_path),
        "extraction_method": f"{EXTRACTOR_NAME}@{tool_version_string()}",
        "extraction_date": today_iso(),
        "pages": extras.get("pages"),
        "headings": extras.get("headings", []),
        "tokens_estimated": count_tokens(body),
        "warnings": warnings,
    }
    write_md(out_path, fm, body)
    emit_success(out_path, body, warnings, extra={"pages": extras.get("pages")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
