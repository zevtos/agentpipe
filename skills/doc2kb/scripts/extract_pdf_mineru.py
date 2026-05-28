#!/usr/bin/env python3
"""
extract_pdf_mineru.py — opt-in VLM-grade PDF extractor for doc2kb.

Wraps the `mineru` CLI (opendatalab/MinerU 2.5+) and reshapes its output to
the same single-file Markdown contract every other extract_*.py honours:
one `<kb_dir>/docs/<doc_id>-<slug>.md` with YAML frontmatter + body, images
copied into `<kb_dir>/assets/` through `save_image_safe`, and a single JSON
line on stdout — `{ok, out, tokens_estimated, warnings, ...}`.

When to use:
    Image-only PDFs (scanned documents, photo-of-page exports), or text-layer
    PDFs whose pymupdf4llm run flagged `mangled_visual_layout` /
    `dropped_pictures` warnings — the VLM backend reads positioned math as
    LaTeX and renders complex layouts more faithfully than text-layer
    extraction can.

    For documents where most pages came out clean from pymupdf4llm but a
    handful of pages dropped vector math/diagrams, use `--pages 2,18-19,35`
    to run mineru only on those pages — orders of magnitude cheaper than
    re-extracting the whole book. Combine with `--patch-into <target.md>`
    to splice the new sections directly into the existing extraction.

When NOT to use:
    Routine text-layer PDFs. pymupdf4llm is faster, runs without ML weights,
    and produces equivalent output for ~80 % of corpora. The strategy
    `mineru` is opt-in via scout's `--enable-mineru` flag or by invoking
    this script directly.

CLI:
    extract_pdf_mineru.py <input_pdf> <output_md>
                          [--doc-id doc-NNN]
                          [--source-rel relative/path/in/corpus.pdf]
                          [--backend auto|pipeline|vlm-auto-engine]
                          [--lang cyrillic|en|ch|...]
                          [--assets-dir <abs_path>]
                          [--assets-rel <rel_prefix_from_md>]
                          [--mineru-cache-dir <abs_path>]
                          [--mineru-bin <path>]
                          [--keep-raw]
                          [--pages 2,18-19,35]
                          [--patch-into TARGET_MD] [--force-patch]
                          [--no-mineru-asset-suffix]

Prerequisites:
    The `mineru` CLI must be installed in the same venv as this script.
    Install via:
        python3 <skill_dir>/scripts/ensure_env.py --tier mineru
    If the CLI is missing the script exits 2 with that exact hint —
    the parent loop should mark the file as `skip` and surface the warning
    rather than crashing the corpus extraction.

Effect:
    1. (Optional, when --pages is set) Slices the input PDF down to the
       requested pages via pymupdf and feeds the subset to mineru. Page
       anchors and asset filenames are remapped back to the original
       page numbers automatically, so the caller never sees subset
       indices.
    2. Runs `mineru -p <input> -o <tmpdir> -b <backend> -l <lang>`.
    3. Locates `<tmpdir>/<stem>/<backend>/<stem>.md` and
       `_content_list.json` (MinerU's stable structured artefact).
    4. Splits the markdown by page_idx → injects `[page N]` anchors
       (remapped to original page numbers when --pages was used).
    5. Copies images from `<tmpdir>/<stem>/<backend>/images/` into
       `<kb_dir>/assets/<doc_id>-pageNN-img<n>.<ext>` via save_image_safe
       and rewrites every Markdown image link in the body to point at
       the renamed asset. When called via --pages, the filenames carry
       an extra `-mineru-` infix
       (`<doc_id>-page<orig:03d>-mineru-img<n>.<ext>`) so the mineru
       assets never collide with pymupdf4llm's existing `<doc_id>-page<NN>-img<n>`
       output for the same original document.
    6. With `--patch-into <target.md>`, splices the extracted page
       sections into the target md in place: each `[page N]` block in
       the target is replaced with the new one, the frontmatter records
       `mineru_patched_pages` and `extraction_method_supplementary`,
       and no extra file is created. Without `--patch-into`, behaves
       like the rest of the extractors and writes a standalone
       `<output_md>` (still page-targeted when `--pages` is set).
    7. Optionally preserves the raw mineru output under
       `<kb_dir>/_mineru/<doc_id>/` so a follow-up `postprocess_popo.py`
       run can consume it (Popo's `post-process/mineru/<run>/` input
       format expects exactly what MinerU emitted, so we avoid having to
       re-extract).

Frontmatter contract:
    Same shape as extract_pdf_pymupdf4llm.py — only `extraction_method`
    distinguishes the backend (`mineru-<backend>@<mineru_version>`).
    Patch mode additionally writes:
      - `mineru_patched_pages: [N, …]` — sorted list of patched pages
      - `extraction_method_supplementary: mineru-<backend>@<version>`
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _common import (  # noqa: E402
    assemble_body_from_sections,
    count_tokens,
    emit_failure,
    emit_success,
    format_page_list,
    log,
    parse_page_list,
    read_body,
    read_frontmatter,
    sanitize_heading,
    save_image_safe,
    sha256_of,
    split_body_by_page_anchors,
    today_iso,
    validate_source_rel,
    write_md,
)


EXTRACTOR_NAME = "mineru"
# Supported backends. MinerU 3.1.x exposes:
#   pipeline           — CPU/GPU CV stack, no VLM, most permissive
#   vlm-auto-engine    — local VLM (vLLM/LMDeploy on Linux, MLX on darwin
#                        when mlx-lm is installed)
#   hybrid-auto-engine — pipeline layout + VLM crops, MinerU's CLI default
#   vlm-http-client    — talks to a remote vlm server
#   hybrid-http-client — talks to a remote hybrid server
# We default to `vlm-auto-engine`, which on Apple Silicon picks the MLX
# backend (`mlx-engine`) via mineru/utils/engine_utils.py. Measured
# back-to-back on M5 Pro / 24 GB, lab2_advanced.pdf (10 pages, math-heavy):
#   vlm-auto-engine    206 s real, clean `$X_{sp}$` LaTeX
#   hybrid-auto-engine 243 s real, `$X _ { s p } ,$` (extra spaces and
#                                    occasional trailing-punct adhesion)
# Both backends sit in the same 3-4 min ballpark on this hardware class
# — VLM inference is the bottleneck regardless of which path mineru
# takes. vlm-auto-engine wins by ~18% wall time AND produces noticeably
# cleaner LaTeX (no spurious spaces in subscripts, no trailing-punct
# adhesion). On CUDA Linux servers without MLX, hybrid is reportedly
# 2-3× faster than VLM — flip the default there if you fork this skill.
# `auto` is accepted as a friendly alias.
SUPPORTED_BACKENDS = (
    "auto",
    "pipeline",
    "vlm-auto-engine",
    "hybrid-auto-engine",
)
_BACKEND_ALIASES = {"auto": "vlm-auto-engine"}
DEFAULT_BACKEND = "vlm-auto-engine"
DEFAULT_LANG = "cyrillic"  # doc2kb users primarily work with RU/EN material
# Time budget for the mineru subprocess. VLM runs at ~0.5–2 s/page on Apple
# Silicon and the pipeline backend at ~1–3 s/page on CPU — even a 500-page
# document fits inside 60 minutes with a generous safety margin.
MINERU_TIMEOUT_SECONDS = 3600

INSTALL_HINT = (
    "MinerU CLI not found. Install the opt-in tier:\n"
    "    python3 <skill_dir>/scripts/ensure_env.py --tier mineru\n"
    "Or install manually into the active venv:\n"
    "    uv pip install -U 'mineru[all]'\n"
    "    # macOS Apple Silicon: also `uv pip install -U mlx mlx-lm`"
)


def _slice_pdf(
    src: Path,
    pages: list[int],
    tmp_dir: Path,
) -> tuple[Path, dict[int, int]]:
    """Carve `pages` (1-indexed, original PDF) into a new tiny PDF inside
    `tmp_dir`. Returns (subset_path, subset_to_original) where
    `subset_to_original[k]` (k 1-indexed in the subset) gives the original
    page number.

    Page numbers above the document's page count or below 1 raise
    ValueError so the caller can surface a clear error to the user.
    """
    import pymupdf  # type: ignore — provided by the lightweight tier

    src_doc = pymupdf.open(str(src))
    try:
        page_count = src_doc.page_count
        invalid = [p for p in pages if p < 1 or p > page_count]
        if invalid:
            raise ValueError(
                f"requested pages out of range for {src.name} "
                f"(1..{page_count}): {invalid}"
            )
        # Stable name keyed on the original stem + selected pages so the
        # `mineru` output filenames stay identifiable in logs.
        digest = hashlib.sha1(
            (src.stem + "|" + ",".join(str(p) for p in pages)).encode()
        ).hexdigest()[:8]
        subset_path = tmp_dir / f"{src.stem}-pages-{digest}.pdf"
        subset_doc = pymupdf.open()
        subset_to_original: dict[int, int] = {}
        for new_index, original_page in enumerate(pages, start=1):
            subset_doc.insert_pdf(
                src_doc,
                from_page=original_page - 1,
                to_page=original_page - 1,
            )
            subset_to_original[new_index] = original_page
        subset_doc.save(str(subset_path))
        subset_doc.close()
    finally:
        src_doc.close()
    return subset_path, subset_to_original


def _mineru_executable(override: str | None = None) -> str | None:
    """Resolve the mineru binary. Prefer an explicit `--mineru-bin` arg,
    then look on PATH (which inside ensure_env.py points at the venv's
    `bin/`). Returns None if mineru isn't available.
    """
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None
    return shutil.which("mineru")


def _mineru_version(binary: str) -> str:
    """Best-effort version string. MinerU's CLI exposes `--version`. We
    fail soft because the rest of the pipeline only needs this for the
    frontmatter audit trail; an unknown version doesn't block extraction.
    """
    try:
        out = subprocess.run(
            [binary, "--version"], capture_output=True, text=True,
            timeout=15, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return "unknown"
    text = (out.stdout or out.stderr or "").strip()
    if not text:
        return "unknown"
    # `mineru, version 3.1.0` → "3.1.0"
    match = re.search(r"\b(\d+\.\d+(?:\.\d+)?)", text)
    return match.group(1) if match else text.splitlines()[0][:32]


def _run_mineru(
    binary: str,
    input_pdf: Path,
    out_dir: Path,
    backend: str,
    lang: str | None,
) -> tuple[int, str, str]:
    """Invoke mineru and return (exit_code, stdout, stderr).

    mineru handles its own progress output on stderr, so we capture it for
    the caller to surface in warnings on failure. Timeout is enforced —
    a stuck VLM job shouldn't block the parent corpus run forever.
    """
    resolved_backend = _BACKEND_ALIASES.get(backend, backend)
    cmd = [binary, "-p", str(input_pdf), "-o", str(out_dir), "-b", resolved_backend]
    if lang:
        cmd.extend(["-l", lang])
    log(f"$ {' '.join(cmd)}", prefix="mineru")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=MINERU_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", (
            f"mineru exceeded the {MINERU_TIMEOUT_SECONDS}-second timeout"
        )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _locate_outputs(out_dir: Path, stem: str, backend: str) -> dict[str, Path]:
    """Find MinerU's per-document outputs.

    MinerU writes `<out_dir>/<stem>/<resolved_backend>/<stem>.md` plus
    `_content_list.json`, `_middle.json`, and an `images/` sibling. The
    backend folder name reflects MinerU's *resolved* backend (e.g. `auto`
    becomes `vlm` or `pipeline` after device detection), so we glob for
    any subdir under `<out_dir>/<stem>/` that contains the markdown rather
    than hard-coding the requested backend name.
    """
    doc_root = out_dir / stem
    found: dict[str, Path] = {}
    if not doc_root.is_dir():
        return found
    for backend_dir in sorted(doc_root.iterdir()):
        if not backend_dir.is_dir():
            continue
        md = backend_dir / f"{stem}.md"
        content_list = backend_dir / f"{stem}_content_list.json"
        if md.is_file() and content_list.is_file():
            found["resolved_backend"] = backend_dir  # type: ignore[assignment]
            found["markdown"] = md
            found["content_list"] = content_list
            middle = backend_dir / f"{stem}_middle.json"
            if middle.is_file():
                found["middle"] = middle
            images = backend_dir / "images"
            if images.is_dir():
                found["images"] = images
            return found
    return found


# MinerU's markdown renders embedded images as Markdown links (`![](images/<hash>.jpg)`)
# or as captioned HTML <img> tags. We catch both so the asset-rewrite pass
# captures every reference even when the source PDF had hand-typed captions.
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(images/([^)\s]+)\)")
_HTML_IMG_RE = re.compile(r'<img\s+[^>]*src=["\']images/([^"\']+)["\']', re.IGNORECASE)


def _copy_images_and_rewrite(
    body: str,
    images_src: Path | None,
    doc_id: str,
    assets_dir: Path,
    assets_rel_prefix: str,
    content_list: list[dict],
    subset_to_original: dict[int, int] | None = None,
    asset_suffix: str = "",
) -> tuple[str, list[str], list[str]]:
    """Copy every image MinerU referenced into the kb assets dir and
    rewrite the body so links point at the new filenames.

    Numbering follows page order (`<doc_id>-pageNN-imgM.<ext>`) so the
    asset names sort alongside the page anchors and match the scheme used
    by extract_pdf_pymupdf4llm.py — second-session agents see one
    consistent naming convention regardless of which backend ran.

    When `subset_to_original` is provided, MinerU's per-subset page indices
    (1..K) are remapped to the original PDF page numbers so callers using
    `--pages 2,34,221` get filenames keyed on the real page (`page002`,
    `page034`, `page221`) rather than the subset index. Pages padded to
    three digits in that mode to leave room for thousand-page books and
    to visually distinguish from pymupdf4llm's two-digit padding.

    `asset_suffix` (e.g. `"-mineru"`) is inserted between the page token
    and `-img<n>` so mineru assets never overwrite an existing pymupdf4llm
    asset with the same (doc_id, page, ordinal) tuple. Default empty
    preserves backward-compatible naming.

    Returns (rewritten_body, asset_relpaths, warnings).
    """
    warnings: list[str] = []
    if images_src is None or not images_src.is_dir():
        return body, [], warnings

    # Build mapping image_basename → (page_idx, ordinal_on_page) from the
    # content list so renamed files keep their page provenance.
    page_seq: dict[str, tuple[int, int]] = {}
    per_page_counter: dict[int, int] = {}
    for entry in content_list:
        if not isinstance(entry, dict):
            continue
        img_path = entry.get("img_path")
        if not isinstance(img_path, str) or not img_path:
            continue
        # MinerU stores paths as `images/<hash>.<ext>` — keep just the base.
        basename = Path(img_path).name
        if basename in page_seq:
            continue
        page_idx_raw = entry.get("page_idx")
        page_no = int(page_idx_raw) + 1 if isinstance(page_idx_raw, int) else 0
        if subset_to_original and page_no in subset_to_original:
            page_no = subset_to_original[page_no]
        ordinal = per_page_counter.get(page_no, 0) + 1
        per_page_counter[page_no] = ordinal
        page_seq[basename] = (page_no, ordinal)

    try:
        assets_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        warnings.append(f"cannot create assets dir {assets_dir}: {e}")
        return body, [], warnings

    rename_map: dict[str, str] = {}
    saved_relpaths: list[str] = []
    seen_relpaths: set[str] = set()
    dedup_cache: dict[str, Path] = {}

    # Padding width for page numbers in asset filenames.
    #
    # In page-patch mode (subset_to_original is set) we always pad to 3
    # digits so successive runs against the same document produce
    # consistent filenames regardless of the subset size — patching pages
    # [28] today and pages [28, 142] tomorrow both write
    # `doc-017-page028-mineru-imgM.ext`. Without this rule the same image
    # would land under `page28` one day and `page028` the next, leaving
    # broken references in the target md.
    #
    # In full-document mode (no subset) we keep the legacy 2-digit
    # behaviour so existing kbs don't see their asset filenames change.
    page_pad = 3 if subset_to_original else 2

    # Copy only images referenced by content_list — MinerU's images/ dir
    # also contains crops used internally by the VLM (formula glyphs,
    # decorative elements) that aren't part of the document content.
    # Without this filter a 10-page lab can leak 30+ ghost assets into
    # the kb (observed on lab2_advanced.pdf: 7 real images vs 32 internal
    # crops). Falling back to all-images would defeat the kb's purpose.
    skipped_unreferenced = 0
    for src in sorted(images_src.iterdir()):
        if not src.is_file():
            continue
        basename = src.name
        if basename not in page_seq:
            skipped_unreferenced += 1
            continue
        try:
            blob = src.read_bytes()
        except OSError as e:
            warnings.append(f"failed to read {basename}: {e}")
            continue
        page_no, ordinal = page_seq[basename]
        ext = src.suffix.lower().lstrip(".") or "jpg"
        target_name = (
            f"{doc_id}-page{page_no:0{page_pad}d}{asset_suffix}-img{ordinal}.{ext}"
        )
        target = assets_dir / target_name
        result = save_image_safe(
            blob, target, dedup_cache=dedup_cache, source_ext=ext,
        )
        if not result:
            if result.reason == "skipped_format":
                warnings.append(
                    f"image {basename}: skipped non-viewable format ({ext})"
                )
            elif result.reason == "skipped_corrupt":
                warnings.append(
                    f"image {basename}: corrupt image bytes — skipped"
                )
            continue
        rel = f"{assets_rel_prefix}/{result.path.name}"
        rename_map[basename] = rel
        if rel not in seen_relpaths:
            seen_relpaths.add(rel)
            saved_relpaths.append(rel)

    if not rename_map:
        return body, saved_relpaths, warnings

    def _rewrite_md(match: re.Match[str]) -> str:
        original_basename = Path(match.group(1)).name
        new_rel = rename_map.get(original_basename)
        if new_rel is None:
            return match.group(0)
        alt = match.group(0).split("![", 1)[1].split("]", 1)[0]
        return f"![{alt}]({new_rel})"

    def _rewrite_html(match: re.Match[str]) -> str:
        original_basename = Path(match.group(1)).name
        new_rel = rename_map.get(original_basename)
        if new_rel is None:
            return match.group(0)
        return match.group(0).replace(
            f"images/{match.group(1)}", new_rel,
        )

    new_body = _MD_IMAGE_RE.sub(_rewrite_md, body)
    new_body = _HTML_IMG_RE.sub(_rewrite_html, new_body)
    return new_body, saved_relpaths, warnings


def _inject_page_anchors(
    body: str,
    content_list: list[dict],
    subset_to_original: dict[int, int] | None = None,
) -> str:
    """Insert `[page N]` markers into the body using the content_list as
    the authoritative page ordering. MinerU's markdown is already laid out
    in reading order but lacks per-page separators; we splice them in by
    matching consecutive text/heading entries against the body and tagging
    page transitions.

    The implementation is conservative: when alignment fails we leave the
    body unchanged rather than fabricating page boundaries — losing the
    anchors is preferable to mis-locating them, since second-session
    agents cite page numbers from these markers.

    When `subset_to_original` is provided, the inserted `[page N]`
    markers use the original PDF page number rather than mineru's
    subset index, so the body reads as if it had been extracted from
    the full document.
    """
    if not content_list:
        return body

    def _orig_page(subset_page: int) -> int:
        if subset_to_original and subset_page in subset_to_original:
            return subset_to_original[subset_page]
        return subset_page

    # Group entries by page in document order.
    transitions: list[int] = []  # 1-based page numbers in encounter order
    last_page: int | None = None
    for entry in content_list:
        if not isinstance(entry, dict):
            continue
        page_idx = entry.get("page_idx")
        if not isinstance(page_idx, int):
            continue
        page_no = page_idx + 1
        if page_no != last_page:
            transitions.append(page_no)
            last_page = page_no

    if not transitions:
        return body

    # Find candidate anchor texts for each transition: prefer the first
    # text-like entry on the page (title or text). We then split body on
    # the first occurrence of that text and insert the page marker.
    pieces: list[str] = []
    cursor = 0
    rendered_pages: set[int] = set()
    for entry in content_list:
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        page_idx = entry.get("page_idx")
        if not isinstance(page_idx, int):
            continue
        page_no = page_idx + 1
        if page_no in rendered_pages:
            continue
        anchor_text: str | None = None
        if entry_type in ("text", "title") and isinstance(entry.get("text"), str):
            anchor_text = entry["text"].strip()
        if not anchor_text or len(anchor_text) < 6:
            continue  # too short to anchor reliably
        # Search forward from cursor for this anchor.
        snippet = anchor_text[:120]
        idx = body.find(snippet, cursor)
        if idx < 0:
            # Try a sanitized version (collapse whitespace) — MinerU
            # sometimes inserts hyphens or newlines mid-sentence.
            compact = re.sub(r"\s+", " ", snippet)
            idx = body.find(compact, cursor)
        if idx < 0:
            continue
        pieces.append(body[cursor:idx])
        pieces.append(f"\n\n[page {_orig_page(page_no)}]\n\n")
        cursor = idx
        rendered_pages.add(page_no)
    pieces.append(body[cursor:])
    rebuilt = "".join(pieces).strip() + "\n"
    # Cleanup: collapse runs of >2 blank lines introduced by the splicing.
    rebuilt = re.sub(r"\n{3,}", "\n\n", rebuilt)
    return rebuilt


def _extract_headings_from_content_list(content_list: list[dict]) -> list[str]:
    """Pull the first ≤10 titles/level-1 entries from content_list so the
    frontmatter `headings:` field matches the layout of pymupdf4llm's
    extraction. Falls back silently when no titles are present."""
    headings: list[str] = []
    for entry in content_list:
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        text_level = entry.get("text_level")
        text = entry.get("text") if isinstance(entry.get("text"), str) else ""
        if entry_type == "text" and isinstance(text_level, int) and text_level >= 1:
            sanitized = sanitize_heading(text)
            if sanitized:
                headings.append(sanitized)
        elif entry_type == "title" and text:
            sanitized = sanitize_heading(text)
            if sanitized:
                headings.append(sanitized)
        if len(headings) >= 10:
            break
    return headings


def _cache_raw_mineru_output(
    backend_dir: Path,
    cache_dir: Path,
    doc_id: str,
) -> Path | None:
    """Preserve MinerU's raw per-document output under
    `<kb_dir>/_mineru/<doc_id>/` so a later opt-in `postprocess_popo.py`
    run can consume it without re-running MinerU. Returns the cache dir
    on success, None on failure (which is non-fatal — Popo just won't be
    available for that doc).
    """
    target = cache_dir / doc_id
    try:
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(backend_dir, target)
    except OSError as e:
        log(f"failed to cache raw mineru output to {target}: {e}",
            prefix="mineru")
        return None
    return target


def _prepare_input(input_pdf: Path, pages: list[int] | None, out_dir: Path,
                   audit: dict) -> tuple[Path, dict | None]:
    """When `pages` is supplied, slice the PDF down to that subset (inside
    `out_dir` so it's cleaned up with the tempdir). Returns
    (target_pdf, subset_to_original_or_None). Updates `audit` in place."""
    if not pages:
        return input_pdf, None
    try:
        target_pdf, subset_to_original = _slice_pdf(input_pdf, pages, out_dir)
    except ValueError as e:
        raise RuntimeError(f"--pages: {e}") from e
    audit["page_subset_requested"] = list(pages)
    audit["page_subset_size"] = len(pages)
    return target_pdf, subset_to_original


def _run_and_locate(binary: str, target_pdf: Path, out_dir: Path,
                    backend: str, lang: str | None) -> dict:
    """Invoke mineru subprocess and locate its produced files. Raises
    RuntimeError on non-zero exit or missing markdown."""
    rc, _stdout, stderr = _run_mineru(binary, target_pdf, out_dir, backend, lang)
    if rc != 0:
        tail = "\n".join((stderr or "").splitlines()[-12:])
        raise RuntimeError(
            f"mineru exited with code {rc}: {tail or 'no stderr captured'}"
        )
    stem = target_pdf.stem
    located = _locate_outputs(out_dir, stem, backend)
    if "markdown" not in located:
        raise RuntimeError(
            f"mineru produced no markdown for {stem}; output tree was "
            f"{[p.name for p in out_dir.iterdir()] if out_dir.exists() else 'missing'}"
        )
    return located


def _read_content_list(located: dict, warnings: list[str]) -> list:
    """Parse content_list.json. Returns an empty list (with warning) on any
    decode/IO failure — callers proceed without anchor remap."""
    try:
        content_list = json.loads(located["content_list"].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        warnings.append(f"content_list.json unreadable: {e}")
        return []
    if not isinstance(content_list, list):
        warnings.append("content_list.json was not a list — page anchors skipped")
        return []
    return content_list


def _compute_page_count(content_list: list,
                        subset_to_original: dict | None) -> int | None:
    """With --pages, "pages" is the size of the patched subset; otherwise
    fall back to the max page_idx+1 from content_list."""
    if subset_to_original:
        return len(subset_to_original)
    max_page = 0
    for entry in content_list:
        if isinstance(entry, dict) and isinstance(entry.get("page_idx"), int):
            max_page = max(max_page, entry["page_idx"] + 1)
    return max_page or None


def _maybe_cache_raw(keep_raw: bool, backend_dir: Path,
                     mineru_cache_dir: Path | None, doc_id: str,
                     audit: dict, warnings: list[str]) -> None:
    """Optionally preserve mineru's raw per-document output so
    postprocess_popo.py can revisit it later. No-op when keep_raw is off
    or no cache dir was supplied."""
    if not (keep_raw and mineru_cache_dir is not None):
        return
    cached = _cache_raw_mineru_output(backend_dir, mineru_cache_dir, doc_id)
    if cached is not None:
        audit["mineru_raw_cache"] = str(cached)
    else:
        warnings.append(
            f"could not preserve raw mineru output for postprocess "
            f"(--keep-raw requested but copy to {mineru_cache_dir} failed)"
        )


def extract(
    input_pdf: Path,
    output_md: Path,
    doc_id: str,
    backend: str,
    lang: str | None,
    assets_dir: Path,
    assets_rel_prefix: str,
    mineru_cache_dir: Path | None,
    mineru_bin: str | None,
    keep_raw: bool,
    pages: list[int] | None = None,
    asset_suffix: str = "",
) -> tuple[str, dict, list[str], dict]:
    """Returns (body, frontmatter_extras, warnings, audit_extras).

    When `pages` is non-empty, the input PDF is sliced down to those
    page numbers before mineru sees it. Page anchors and asset filenames
    in the returned body are remapped back to the original numbering so
    callers and downstream agents never observe subset indices.
    """
    warnings: list[str] = []
    extras: dict = {}
    audit: dict = {}

    binary = _mineru_executable(mineru_bin)
    if binary is None:
        raise FileNotFoundError(INSTALL_HINT)

    audit["mineru_version"] = _mineru_version(binary)
    audit["mineru_backend_requested"] = backend

    with tempfile.TemporaryDirectory(prefix="mineru-") as tmp:
        out_dir = Path(tmp)

        target_pdf, subset_to_original = _prepare_input(
            input_pdf, pages, out_dir, audit,
        )

        located = _run_and_locate(binary, target_pdf, out_dir, backend, lang)
        backend_dir = located["resolved_backend"]
        audit["mineru_backend_resolved"] = backend_dir.name

        body = located["markdown"].read_text(encoding="utf-8")
        content_list = _read_content_list(located, warnings)

        body = _inject_page_anchors(body, content_list, subset_to_original)

        body, asset_relpaths, image_warnings = _copy_images_and_rewrite(
            body=body,
            images_src=located.get("images"),
            doc_id=doc_id,
            assets_dir=assets_dir,
            assets_rel_prefix=assets_rel_prefix,
            content_list=content_list,
            subset_to_original=subset_to_original,
            asset_suffix=asset_suffix,
        )
        warnings.extend(image_warnings)
        if asset_relpaths:
            extras["assets"] = asset_relpaths

        extras["headings"] = _extract_headings_from_content_list(content_list)
        pages_count = _compute_page_count(content_list, subset_to_original)
        if pages_count:
            extras["pages"] = pages_count

        _maybe_cache_raw(keep_raw, backend_dir, mineru_cache_dir, doc_id,
                         audit, warnings)

    return body, extras, warnings, audit


def _splice_into_target(
    target_path: Path,
    new_body: str,
    patched_pages: list[int],
    new_assets: list[str],
    extraction_method_supplementary: str,
    new_warnings: list[str],
    *,
    source_sha256: str,
    force: bool,
) -> tuple[Path, list[str]]:
    """Replace `[page N]` sections in the target md with the new ones for
    each page in `patched_pages`. Returns (target_path, merged_warnings).

    Safety:
      - Refuses to splice if the target frontmatter's `source_sha256` does
        not match the input PDF's sha256 (unless `force=True`). This is
        the only check that catches "user pointed --patch-into at the
        wrong document".
      - Refuses if the target body has no `[page N]` anchors at all — we
        can't splice into a page-anchorless body without guessing where
        sections belong.
      - Pages requested but not present in the target are reported as
        warnings; their content from the patch is appended at the end
        of the body in numeric order so the agent can still find them.

    Frontmatter merge:
      - `mineru_patched_pages` becomes the union of the existing list
        (if any) and `patched_pages`.
      - `extraction_method_supplementary` is set to the new mineru tag.
      - `tokens_estimated` is recomputed from the merged body.
      - `assets` is the union of the existing list (if any) and the new
        patches; dedup preserves first-seen order.
      - `warnings` accumulates a single line noting the splice plus any
        warnings the patch run produced.
    """
    if not target_path.is_file():
        raise RuntimeError(f"--patch-into target not found: {target_path}")

    target_fm = read_frontmatter(target_path)
    target_body = read_body(target_path)
    if not target_fm:
        raise RuntimeError(
            f"--patch-into target {target_path.name} has no readable YAML "
            "frontmatter"
        )

    target_sha = (target_fm.get("source_sha256") or "").strip().lower()
    if target_sha and target_sha != source_sha256.lower() and not force:
        raise RuntimeError(
            f"--patch-into target source_sha256 {target_sha!r} does not "
            f"match input PDF sha256 {source_sha256!r}. Pass --force-patch "
            "to override (intended only when the input PDF is a known-good "
            "re-export of the same document)."
        )

    preamble, target_sections = split_body_by_page_anchors(target_body)
    if not target_sections:
        raise RuntimeError(
            f"--patch-into target {target_path.name} has no '[page N]' "
            "anchors; cannot splice. Re-extract the source PDF first via "
            "extract_pdf_pymupdf4llm.py to lay down the anchors."
        )

    _new_preamble, new_sections = split_body_by_page_anchors(new_body)
    # When the patch run produced no anchors at all (unlikely but
    # possible on a single-page subset where mineru couldn't align),
    # treat the entire new body as a single section keyed on the only
    # patched page.
    if not new_sections and len(patched_pages) == 1:
        only = patched_pages[0]
        new_sections = {only: f"[page {only}]\n\n{new_body.strip()}"}

    missing_in_patch: list[int] = []
    missing_in_target: list[int] = []
    splice_warnings: list[str] = []

    for page in sorted(set(patched_pages)):
        if page not in new_sections:
            missing_in_patch.append(page)
            continue
        if page not in target_sections:
            missing_in_target.append(page)
        target_sections[page] = new_sections[page]

    if missing_in_patch:
        splice_warnings.append(
            "mineru_patch: requested pages "
            f"[{format_page_list(missing_in_patch)}] were not present in the "
            "mineru output — splice skipped for those pages"
        )
    if missing_in_target:
        splice_warnings.append(
            "mineru_patch: pages "
            f"[{format_page_list(missing_in_target)}] were not present in "
            "the target md; new sections were appended in numeric order"
        )

    rebuilt = assemble_body_from_sections(preamble, target_sections)

    # Frontmatter merge.
    existing_patched = target_fm.get("mineru_patched_pages") or []
    merged_pages_set: set[int] = set()
    for entry in list(existing_patched) + list(patched_pages):
        try:
            merged_pages_set.add(int(entry))
        except (TypeError, ValueError):
            continue
    merged_patched_pages = sorted(merged_pages_set)

    existing_assets = target_fm.get("assets") or []
    if not isinstance(existing_assets, list):
        existing_assets = [str(existing_assets)]
    merged_assets: list[str] = []
    seen_assets: set[str] = set()
    for a in list(existing_assets) + list(new_assets):
        a_str = str(a)
        if a_str and a_str not in seen_assets:
            seen_assets.add(a_str)
            merged_assets.append(a_str)

    existing_warnings = target_fm.get("warnings") or []
    if not isinstance(existing_warnings, list):
        existing_warnings = [str(existing_warnings)]
    merged_warnings = list(existing_warnings)
    splice_note = (
        f"mineru_patch: replaced {len(patched_pages)} page section(s) "
        f"[{format_page_list(patched_pages)}] via "
        f"{extraction_method_supplementary}"
    )
    merged_warnings.append(splice_note)
    merged_warnings.extend(splice_warnings)
    merged_warnings.extend(
        f"mineru_patch[{format_page_list(patched_pages)}]: {w}"
        for w in new_warnings
    )

    target_fm["mineru_patched_pages"] = merged_patched_pages
    target_fm["extraction_method_supplementary"] = extraction_method_supplementary
    target_fm["extraction_date"] = today_iso()
    if merged_assets:
        target_fm["assets"] = merged_assets
    target_fm["warnings"] = merged_warnings
    target_fm["tokens_estimated"] = count_tokens(rebuilt)

    write_md(target_path, target_fm, rebuilt)
    return target_path, splice_warnings


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Extract PDF → Markdown via the opt-in MinerU VLM backend.",
    )
    ap.add_argument("input_pdf")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    ap.add_argument(
        "--backend",
        default=DEFAULT_BACKEND,
        choices=SUPPORTED_BACKENDS,
        help=f"MinerU parsing backend (default: {DEFAULT_BACKEND}).",
    )
    ap.add_argument(
        "--lang",
        default=DEFAULT_LANG,
        help=f"Language hint passed to mineru -l (default: {DEFAULT_LANG}; "
             "use 'en' for English-only PDFs, 'ch' for Chinese, etc.).",
    )
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
             "saved images (default: '../assets').",
    )
    ap.add_argument(
        "--mineru-cache-dir",
        default=None,
        help="Absolute directory under which raw mineru output is cached "
             "when --keep-raw is set (default: <output_md>.parent.parent/_mineru).",
    )
    ap.add_argument(
        "--mineru-bin",
        default=None,
        help="Explicit path to the mineru executable (default: resolved via "
             "PATH). Useful when invoking from outside ensure_env.py.",
    )
    ap.add_argument(
        "--keep-raw",
        action="store_true",
        help="Preserve MinerU's raw per-document output under the cache "
             "dir for follow-up postprocess_popo.py runs.",
    )
    ap.add_argument(
        "--pages",
        default=None,
        help="Page-range patching: comma-separated list of 1-based page "
             "numbers with optional ranges, e.g. '2,18-19,35,221'. Only "
             "those pages are sent to mineru (via an internal subset PDF) "
             "and the returned `[page N]` anchors and asset filenames are "
             "remapped to the original page numbers. Use this to recover "
             "vector math/diagrams that pymupdf4llm dropped on specific "
             "pages without re-extracting the whole document.",
    )
    ap.add_argument(
        "--patch-into",
        default=None,
        help="Target markdown file to splice the extracted page sections "
             "into. Requires --pages. Each requested page's section in the "
             "target is replaced by the mineru-extracted one; the target's "
             "frontmatter is updated (mineru_patched_pages, "
             "extraction_method_supplementary). No standalone output file "
             "is written. The target's source_sha256 must match the input "
             "PDF unless --force-patch is set.",
    )
    ap.add_argument(
        "--force-patch",
        action="store_true",
        help="Bypass the source_sha256 match check when --patch-into "
             "targets a markdown produced from a different but "
             "content-equivalent PDF re-export.",
    )
    ap.add_argument(
        "--no-mineru-asset-suffix",
        action="store_true",
        help="Disable the `-mineru` infix in asset filenames produced by "
             "page-patching mode. Use only when you control the assets "
             "directory and know there is no pymupdf4llm output to collide "
             "with. The default (suffix on) is safe in mixed extractions.",
    )
    return ap


class _ArgsError(Exception):
    """argparse-stage failure that should become emit_failure + exit code.
    Carries (reason, exit_code) so the caller can re-emit without
    duplicating each early-return branch."""
    def __init__(self, reason: str, exit_code: int = 1):
        super().__init__(reason)
        self.reason = reason
        self.exit_code = exit_code


def _resolve_args(args) -> dict:
    """Resolve CLI args to absolute paths + parsed --pages list. Raises
    _ArgsError when the CLI is in a bad shape (returns (reason, code) for
    main to surface)."""
    in_path = Path(args.input_pdf).expanduser().resolve()
    out_path = Path(args.output_md).expanduser().resolve()
    source_rel = args.source_rel or in_path.name
    try:
        source_rel = validate_source_rel(source_rel)
    except ValueError as e:
        raise _ArgsError(f"invalid --source-rel: {e}") from e

    if not in_path.is_file():
        raise _ArgsError(f"input not found: {in_path}")

    # --patch-into without --pages would replace whole documents in place,
    # losing the existing extraction's structure. Refuse rather than
    # silently doing something destructive.
    if args.patch_into and not args.pages:
        raise _ArgsError(
            "--patch-into requires --pages; refusing to splice an entire "
            "document over a target md"
        )

    pages: list[int] | None = None
    if args.pages:
        try:
            pages = parse_page_list(args.pages)
        except ValueError as e:
            raise _ArgsError(f"invalid --pages: {e}") from e

    if args.assets_dir:
        assets_dir = Path(args.assets_dir).expanduser().resolve()
    else:
        assets_dir = out_path.parent.parent / "assets"

    if args.mineru_cache_dir:
        cache_dir = Path(args.mineru_cache_dir).expanduser().resolve()
    else:
        cache_dir = out_path.parent.parent / "_mineru"

    asset_suffix = "" if (args.no_mineru_asset_suffix or not pages) else "-mineru"

    return {
        "in_path": in_path,
        "out_path": out_path,
        "source_rel": source_rel,
        "pages": pages,
        "assets_dir": assets_dir,
        "cache_dir": cache_dir,
        "asset_suffix": asset_suffix,
    }


def _call_extract(args, resolved: dict
                  ) -> tuple[str, dict, list[str], dict] | int:
    """Wrap extract() with the 3 exception handlers. Returns the extract
    tuple on success or an int exit code on failure (after emitting
    failure JSON)."""
    in_path = resolved["in_path"]
    try:
        return extract(
            input_pdf=in_path,
            output_md=resolved["out_path"],
            doc_id=args.doc_id,
            backend=args.backend,
            lang=args.lang,
            assets_dir=resolved["assets_dir"],
            assets_rel_prefix=args.assets_rel,
            mineru_cache_dir=resolved["cache_dir"],
            mineru_bin=args.mineru_bin,
            keep_raw=args.keep_raw,
            pages=resolved["pages"],
            asset_suffix=resolved["asset_suffix"],
        )
    except FileNotFoundError as e:
        # mineru binary missing — exit 2 so the parent loop can mark the
        # file as "needs install" and skip without treating this as a
        # corrupt-PDF failure.
        emit_failure(str(e), extra={"input": str(in_path)})
        return 2
    except RuntimeError as e:
        emit_failure(f"extraction failed: {e}", extra={"input": str(in_path)})
        return 1
    except Exception as e:  # noqa: BLE001 — defensive: subprocess wrappers
        emit_failure(
            f"extraction crashed: {type(e).__name__}: {e}",
            extra={"input": str(in_path)},
        )
        return 1


def _run_patch_mode(args, resolved: dict, body: str, extras: dict,
                    warnings: list[str], audit: dict,
                    extraction_method: str, input_sha256: str) -> int:
    """--patch-into: splice extracted pages into an existing markdown."""
    target_path = Path(args.patch_into).expanduser().resolve()
    pages = resolved["pages"] or []
    try:
        written_path, splice_warnings = _splice_into_target(
            target_path=target_path,
            new_body=body,
            patched_pages=pages,
            new_assets=extras.get("assets", []),
            extraction_method_supplementary=extraction_method,
            new_warnings=warnings,
            source_sha256=input_sha256,
            force=args.force_patch,
        )
    except RuntimeError as e:
        emit_failure(f"--patch-into: {e}",
                     extra={"input": str(resolved["in_path"])})
        return 1

    success_extras: dict = {
        "patched_pages": pages,
        "patched_into": str(written_path),
        "assets_extracted": len(extras.get("assets", [])),
        "extraction_method_supplementary": extraction_method,
    }
    success_extras.update(audit)
    # Recompute body from disk so the token count reflects the spliced-in
    # size, not the patch alone.
    merged_body = read_body(written_path)
    emit_success(written_path, merged_body, warnings + splice_warnings,
                 extra=success_extras)
    return 0


def _run_standalone_mode(args, resolved: dict, body: str, extras: dict,
                         warnings: list[str], audit: dict,
                         extraction_method: str, input_sha256: str) -> int:
    """No --patch-into: write a fresh extraction to out_path."""
    out_path = resolved["out_path"]
    pages = resolved["pages"]
    fm = {
        "id": args.doc_id,
        "source": resolved["source_rel"],
        "source_type": "pdf",
        "source_sha256": input_sha256,
        "extraction_method": extraction_method,
        "extraction_date": today_iso(),
        "pages": extras.get("pages"),
        "headings": extras.get("headings", []),
        "tokens_estimated": count_tokens(body),
        "warnings": warnings,
    }
    if pages:
        fm["mineru_subset_pages"] = pages
    if extras.get("assets"):
        fm["assets"] = extras["assets"]
    write_md(out_path, fm, body)

    success_extras: dict = {
        "pages": extras.get("pages"),
        "assets_extracted": len(extras.get("assets", [])),
    }
    if pages:
        success_extras["mineru_subset_pages"] = pages
    success_extras.update(audit)
    emit_success(out_path, body, warnings, extra=success_extras)
    return 0


def main() -> int:
    args = _build_arg_parser().parse_args()
    try:
        resolved = _resolve_args(args)
    except _ArgsError as e:
        emit_failure(e.reason)
        return e.exit_code

    extract_result = _call_extract(args, resolved)
    if isinstance(extract_result, int):
        return extract_result
    body, extras, warnings, audit = extract_result

    backend_tag = audit.get("mineru_backend_resolved") or args.backend
    extraction_method = (
        f"{EXTRACTOR_NAME}-{backend_tag}@{audit.get('mineru_version', 'unknown')}"
    )
    input_sha256 = sha256_of(resolved["in_path"])

    if args.patch_into:
        return _run_patch_mode(args, resolved, body, extras, warnings, audit,
                               extraction_method, input_sha256)
    return _run_standalone_mode(args, resolved, body, extras, warnings, audit,
                                extraction_method, input_sha256)


if __name__ == "__main__":
    sys.exit(main())
