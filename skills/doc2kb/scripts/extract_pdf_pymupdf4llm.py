#!/usr/bin/env python3
"""
extract_pdf_pymupdf4llm.py — Phase 4 extractor for native (text-layer) PDFs.

CLI:
    extract_pdf_pymupdf4llm.py <input_pdf> <output_md>
                              [--doc-id doc-NNN]
                              [--source-rel relative/path/in/corpus.pdf]
                              [--assets-dir <abs_path>]
                              [--assets-rel <rel_prefix_from_md>]
                              [--no-extract-images]

Effect:
    Writes <output_md> with YAML frontmatter + Markdown body. Page bodies are
    separated by `[page N]` anchors (research §3.4). Stdout receives a single
    JSON line summarizing the result.

Image handling:
    - By default, the extractor scans the PDF for embedded images and
      replaces pymupdf4llm's `==> picture [WxH] intentionally omitted <==`
      placeholders with Markdown image links pointing to JPEG/PNG files
      saved under `<kb_dir>/assets/`. This recovers formulas, comparison
      matrices, and result charts that pymupdf4llm cannot represent in
      text. Use `--no-extract-images` to disable.
    - The mapping placeholder→image uses pymupdf4llm's per-page emission
      order, which matches `page.get_images(full=True)` in practice.
    - The `dropped_pictures` warning is only emitted for placeholders that
      could NOT be substituted (caller should re-run with a Read-tool
      transcription as a last resort).

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
    _RESIDUAL_LIGATURE_RE,
    clean_whitespace,
    count_tokens,
    emit_failure,
    emit_success,
    log,
    recover_ligatures,
    sanitize_heading,
    save_image_safe,
    sha256_of,
    strip_page_footer_numbers,
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


def _replace_dropped_pictures_with_assets(
    body: str,
    input_pdf: Path,
    doc_id: str,
    assets_dir: Path,
    assets_rel_prefix: str,
) -> tuple[str, list[str], int, int, list[str]]:
    """Extract embedded images from the PDF and substitute pymupdf4llm's
    `==> picture [WxH] intentionally omitted <==` placeholders with
    Markdown image links pointing to the saved files. Images that pymupdf4llm
    knows about but doesn't emit a placeholder for — most often table-cell
    icons in BPMN-style reference docs — are appended to the bottom of each
    `[page N]` section under an `### Additional page N images` heading so
    they're not silently lost.

    Numbering: images are saved as `<doc_id>-pageNN-imgN.<ext>` and
    placeholders are matched in pymupdf4llm's emission order, which
    mirrors pymupdf's `page.get_images(full=True)` order. When the two
    diverge (common in table-heavy PDFs), unmatched images surface in the
    additional-images block rather than being silently lost.

    Every image goes through save_image_safe(), enforcing the 2000-px
    max-dimension cap and the decorative-pixel filter. Identical images
    (logos, repeated icons) deduplicate to the first saved copy.

    Returns (new_body, saved_relpaths, replaced_count, total_placeholders,
             extractor_warnings).
    """
    import pymupdf  # type: ignore

    total = len(PICTURE_PLACEHOLDER_RE.findall(body))
    warnings: list[str] = []

    try:
        assets_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"cannot create assets dir {assets_dir}: {e}") from e

    try:
        doc = pymupdf.open(str(input_pdf))
    except Exception as e:
        raise RuntimeError(f"failed to reopen pdf for image extraction: {e}") from e

    # saved_per_page[page_no] = list[(image_idx, relpath, save_reason)]
    saved_per_page: dict[int, list[tuple[int, str, str]]] = {}
    saved_relpaths: list[str] = []
    seen_relpaths: set[str] = set()
    dedup_cache: dict = {}
    try:
        for page_no in range(1, doc.page_count + 1):
            page = doc[page_no - 1]
            for idx, img_info in enumerate(page.get_images(full=True), 1):
                xref = img_info[0]
                try:
                    info = doc.extract_image(xref)
                except Exception:
                    continue
                ext = (info.get("ext") or "png").lower()
                blob = info.get("image")
                if not blob:
                    continue
                filename = f"{doc_id}-page{page_no:02d}-img{idx}.{ext}"
                target = assets_dir / filename
                result = save_image_safe(
                    blob, target, dedup_cache=dedup_cache, source_ext=ext,
                )
                if not result:
                    if result.reason == "skipped_format":
                        warnings.append(
                            f"page {page_no} image {idx}: skipped non-viewable "
                            f"format ({ext})"
                        )
                    elif result.reason == "skipped_corrupt":
                        warnings.append(
                            f"page {page_no} image {idx}: corrupt image bytes — skipped"
                        )
                    # too-small: silent (decorative pixel)
                    continue
                rel = f"{assets_rel_prefix}/{result.path.name}"
                saved_per_page.setdefault(page_no, []).append(
                    (idx, rel, result.reason)
                )
                if rel not in seen_relpaths:
                    seen_relpaths.add(rel)
                    saved_relpaths.append(rel)
    finally:
        doc.close()

    if not saved_per_page:
        return body, [], 0, total, warnings

    page_anchor_re = re.compile(r"\[page (\d+)\]")
    anchors = list(page_anchor_re.finditer(body))
    if not anchors:
        return body, saved_relpaths, 0, total, warnings

    replaced = 0
    out: list[str] = [body[: anchors[0].start()]]
    for i, anchor in enumerate(anchors):
        page_no = int(anchor.group(1))
        section_end = anchors[i + 1].start() if i + 1 < len(anchors) else len(body)
        section = body[anchor.start():section_end]

        page_imgs = saved_per_page.get(page_no, [])
        used_idx: set[int] = set()
        counter = {"n": 0}

        def _sub(_match, _page_no=page_no, _counter=counter, _imgs=page_imgs,
                 _used=used_idx):
            nonlocal replaced
            _counter["n"] += 1
            # Look up the n-th image by its original idx (1-based).
            target = next((t for t in _imgs if t[0] == _counter["n"]), None)
            if target is None:
                return _match.group(0)
            _used.add(target[0])
            replaced += 1
            return (
                f"![page {_page_no}, image {_counter['n']}]({target[1]})"
            )

        substituted = PICTURE_PLACEHOLDER_RE.sub(_sub, section)

        # Any images for this page that weren't referenced by a placeholder
        # belong to table cells, watermarks, or otherwise sit outside the
        # main text flow. Surface them in an explicit block so a downstream
        # agent doesn't miss the BPMN icon next to a definition or a
        # diagram embedded inside a table cell.
        extras_block_lines: list[str] = []
        for idx, rel, _reason in page_imgs:
            if idx in used_idx:
                continue
            extras_block_lines.append(
                f"![page {page_no}, image {idx} (additional)]({rel})"
            )
        if extras_block_lines:
            # Trim a trailing newline from substituted so the heading sits
            # on its own paragraph after the body text.
            section_text = substituted.rstrip("\n")
            section_text += "\n\n"
            section_text += f"#### Additional embedded images on page {page_no}\n\n"
            section_text += "\n\n".join(extras_block_lines) + "\n\n"
            out.append(section_text)
        else:
            out.append(substituted)

    new_body = "".join(out)
    return new_body, saved_relpaths, replaced, total, warnings


def extract(
    input_pdf: Path,
    doc_id: str = "doc-000",
    assets_dir: Path | None = None,
    assets_rel_prefix: str = "../assets",
    extract_images: bool = True,
) -> tuple[str, dict, list[str]]:
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

    # pymupdf4llm ≤1.27.x drops one letter from fi/ff/fl ligature glyphs
    # whose PDF ToUnicode CMap maps to a single ASCII char. Raw
    # pymupdf.Page.get_text returns the words correctly; the bug is in
    # pymupdf4llm's higher-level reassembly. Apply our known-pattern fix
    # list and surface a warning whenever any replacement fires, so the
    # operator can see that the body was healed automatically.
    body, lig_fixed = recover_ligatures(body)
    if lig_fixed:
        warnings.append(
            f"ligatures_recovered: pymupdf4llm dropped letters from {lig_fixed} "
            "word(s) with fi/ff/fl glyphs (e.g. 'Ofcial'→'Official', "
            "'fexible'→'flexible'); fixed via the doc2kb ligature-recovery "
            "table — no manual action needed"
        )
        residual = _RESIDUAL_LIGATURE_RE.findall(body)
        # Filter to *new* broken patterns: any residual that looks like
        # `f<consonant>` inside a real word and that wasn't already a
        # known English `aft`/`oft`/`soft` cluster (regex covers that).
        # Cap the sample at 5 to keep the warning short.
        if residual:
            sample = sorted(set(residual))[:5]
            warnings.append(
                f"ligature_residual: {len(residual)} suspicious word(s) "
                f"with f-consonant patterns still present (sample: "
                f"{', '.join(sample)}); if these are broken ligatures, "
                "extend _LIGATURE_FIXES in scripts/_common.py"
            )

    # Drop standalone footer page numbers that sit between consecutive
    # [page N] anchors. normalize_md's detect_recurring_lines cannot see
    # them because each footer is unique (1, 2, 3, …), so we strip them
    # here using a positional pattern.
    body, footer_stripped = strip_page_footer_numbers(body)
    if footer_stripped:
        log(f"stripped {footer_stripped} footer page number(s)")

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

    # Dropped-pictures recovery. pymupdf4llm replaces positioned figures with
    # `==> picture [WxH] intentionally omitted <==` placeholders. In
    # scientific/lab PDFs these placeholders typically hide equations,
    # comparison matrices, or block diagrams that the body text references.
    # If image extraction is enabled (default), pull the underlying images
    # out of the PDF via pymupdf and substitute Markdown image links so the
    # binary content lives on disk under `<kb_dir>/assets/`. The
    # `dropped_pictures` warning is suppressed for placeholders that were
    # successfully recovered.
    if extract_images and assets_dir is not None:
        try:
            (body, asset_relpaths, replaced, total_drops,
             image_warnings) = _replace_dropped_pictures_with_assets(
                body=body,
                input_pdf=input_pdf,
                doc_id=doc_id,
                assets_dir=assets_dir,
                assets_rel_prefix=assets_rel_prefix,
            )
            warnings.extend(image_warnings)
            if asset_relpaths:
                extras["assets"] = asset_relpaths
        except RuntimeError as e:
            warnings.append(f"image extraction failed: {e}")
            replaced, total_drops = 0, len(PICTURE_PLACEHOLDER_RE.findall(body))
    else:
        replaced, total_drops = 0, len(PICTURE_PLACEHOLDER_RE.findall(body))

    # Re-run the heuristic on the (possibly substituted) body. If any
    # placeholders remain above the threshold, emit the same loud warning
    # the operator can act on.
    dropped = _detect_dropped_pictures(body, n_pages)
    if dropped is not None:
        remediation = (
            "Re-extract this PDF by reading the source file directly via "
            "the Read tool and overwrite the body with a manual "
            "transcription of the remaining figures"
        )
        if replaced > 0:
            warnings.append(
                f"dropped_pictures: {replaced}/{total_drops} placeholder(s) "
                f"were auto-recovered as assets, but {dropped['count']} "
                f"still remain over {dropped['pages']} page(s) "
                f"({dropped['per_page']}/page). " + remediation
            )
        else:
            warnings.append(
                f"dropped_pictures: pymupdf4llm omitted {dropped['count']} "
                f"picture(s) over {dropped['pages']} page(s) "
                f"({dropped['per_page']}/page); in scientific/lab PDFs "
                "these placeholders typically hide equations, matrices, "
                "or block diagrams — body text will reference formulas "
                "that aren't in the extraction. " + remediation
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
    ap.add_argument(
        "--assets-dir",
        default=None,
        help="Absolute directory to write extracted images into. Defaults "
             "to <output_md>.parent.parent/assets (= <kb_dir>/assets).",
    )
    ap.add_argument(
        "--assets-rel",
        default="../assets",
        help="Relative prefix used inside the Markdown body to link the "
             "saved images (default: '../assets', matching the standard "
             "kb_dir/docs/*.md → kb_dir/assets/ layout).",
    )
    ap.add_argument(
        "--no-extract-images",
        action="store_true",
        help="Disable embedded image extraction; keep pymupdf4llm's "
             "intentionally-omitted placeholders verbatim and re-enable "
             "the original dropped_pictures warning.",
    )
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

    if args.no_extract_images:
        assets_dir: Path | None = None
    elif args.assets_dir:
        assets_dir = Path(args.assets_dir).expanduser().resolve()
    else:
        # Default: <output_md>.parent.parent / "assets". Standard layout is
        # <kb_dir>/docs/<file>.md so two ascents land on <kb_dir>.
        assets_dir = out_path.parent.parent / "assets"

    try:
        body, extras, warnings = extract(
            in_path,
            doc_id=args.doc_id,
            assets_dir=assets_dir,
            assets_rel_prefix=args.assets_rel,
            extract_images=not args.no_extract_images,
        )
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
    if extras.get("assets"):
        fm["assets"] = extras["assets"]
    write_md(out_path, fm, body)
    emit_success(out_path, body, warnings, extra={
        "pages": extras.get("pages"),
        "assets_extracted": len(extras.get("assets", [])),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
