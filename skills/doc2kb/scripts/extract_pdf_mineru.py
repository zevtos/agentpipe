#!/usr/bin/env python3
"""
extract_pdf_mineru.py — opt-in VLM-grade PDF extractor for doc2kb.

Drives MinerU 4 (opendatalab/MinerU, `standard` tier: small layout models +
the MinerU2.5-Pro VLM) through `_mineru_driver.py`, which runs inside the
tier's own venv, and reshapes its output to the same single-file Markdown
contract every other extract_*.py honours: one
`<kb_dir>/docs/<doc_id>-<slug>.md` with YAML frontmatter + body, images
copied into `<kb_dir>/assets/` through `save_image_safe`, and a single JSON
line on stdout — `{ok, out, tokens_estimated, warnings, ...}`.

When to use:
    Image-only PDFs (scanned documents, photo-of-page exports), or text-layer
    PDFs whose pymupdf4llm run flagged `mangled_visual_layout` /
    `dropped_pictures` warnings — the VLM reads positioned math as LaTeX and
    renders complex layouts more faithfully than text-layer extraction can.

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
                          [--tier standard|advanced|basic]
                          [--ocr-mode ocr|auto|txt]
                          [--assets-dir <abs_path>]
                          [--assets-rel <rel_prefix_from_md>]
                          [--mineru-cache-dir <abs_path>]
                          [--mineru-python <path>]
                          [--keep-raw]
                          [--pages 2,18-19,35]
                          [--patch-into TARGET_MD] [--force-patch]
                          [--no-mineru-asset-suffix]
    Legacy 3.x flags still accepted: `--backend` (vlm-auto-engine,
    hybrid-auto-engine, auto, pipeline → all run `standard`), `--lang`
    (MinerU 4 has no OCR language setting; ignored with a warning) and
    `--mineru-bin` (its sibling `python` is used).

Prerequisites:
    The tier venv (<state>/mineru/venv) with MinerU 4 and its models:
        python3 <skill_dir>/scripts/ensure_env.py --tier mineru
    If it is missing the script exits 2 with that exact hint — the parent
    loop should mark the file as `skip` and surface the warning rather than
    crashing the corpus extraction.

Effect:
    1. Runs `<tier-venv>/python -I _mineru_driver.py parse <input> --out <tmp>
       --tier <tier> --ocr-mode <mode> [--pages …]`. `--pages` goes to MinerU's
       native page_range; MinerU keeps ORIGINAL page indices in its output, so
       nothing is sliced or remapped.
    2. Reads `<tmp>/markdown.md` and `<tmp>/content_list.json` (Content List
       V1, rendered by the driver from the saved middle JSON).
    3. Splits the markdown by page_idx → injects `[page N]` anchors.
    4. Copies the images the markdown links (`images/page_<i>_<kind>_<n>.<ext>`)
       into `<kb_dir>/assets/<doc_id>-pageNN-img<n>.<ext>` via save_image_safe
       and rewrites the links. Equation/table crops that the body renders as
       LaTeX/HTML are not copied. When called via --pages, the filenames
       carry a `-mineru-` infix (`<doc_id>-page<orig:03d>-mineru-img<n>.<ext>`)
       so they never collide with pymupdf4llm's `<doc_id>-page<NN>-img<n>`.
    5. With `--patch-into <target.md>`, splices the extracted page sections
       into the target md in place: each `[page N]` block in the target is
       replaced with the new one, the frontmatter records
       `mineru_patched_pages` and `extraction_method_supplementary`, and no
       extra file is created. Without `--patch-into`, writes a standalone
       `<output_md>` (still page-targeted when `--pages` is set).
    6. With `--keep-raw`, preserves MinerU's output under
       `<kb_dir>/_mineru/<doc_id>/vlm/` and adds the Content List V1 as
       `<doc_id>_content_list.json` — the name MinerU-Popo's MinerU reader
       looks up — so a follow-up `postprocess_popo.py` run can consume it
       without re-extracting. MinerU 4's middle JSON (schema 2.0) is kept as
       `middle_json.json`, never as `<doc_id>_middle.json`, which Popo would
       parse with its 3.x reader.

Frontmatter contract:
    Same shape as extract_pdf_pymupdf4llm.py — only `extraction_method`
    distinguishes the backend (`mineru-<tier>@<mineru_version>`).
    Patch mode additionally writes:
      - `mineru_patched_pages: [N, …]` — sorted list of patched pages
      - `extraction_method_supplementary: mineru-<tier>@<version>`
"""
from __future__ import annotations

import argparse
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
    skipped_image_note,
    sha256_of,
    split_body_by_page_anchors,
    today_iso,
    validate_source_rel,
    write_md,
)


EXTRACTOR_NAME = "mineru"
DRIVER = Path(__file__).resolve().with_name("_mineru_driver.py")
# MinerU 4 quality tiers (the `flash` tier is text-layer only and not offered).
# Measured on 30 Russian pages (scanned ГОСТ, LaTeX lab report, two slide
# decks), Apple M5 Pro / 24 GB, see references/mineru.md:
#   standard  0.20 % Cyrillic CER, 9/11 formulas, 3.5 s/page (llama.cpp VLM)
#   basic     no Cyrillic at all — its OCR model has no Cyrillic recognizer,
#             Russian text comes out as bare digits and punctuation
# `advanced` uses the standard models with more inference compute.
SUPPORTED_TIERS = ("standard", "advanced", "basic")
DEFAULT_TIER = "standard"
# 3.x backend names, still accepted from old `_overrides.json` files and CLIs.
# `pipeline` maps to standard as well: its 4.x successor (`basic`) would
# silently drop every Cyrillic word.
_LEGACY_BACKEND_TIERS = {
    "auto": "standard",
    "vlm-auto-engine": "standard",
    "hybrid-auto-engine": "standard",
    "pipeline": "standard",
}
SUPPORTED_BACKENDS = tuple(_LEGACY_BACKEND_TIERS)
# "ocr": the VLM reads every block. "auto" would take text from the PDF text
# layer, which keeps line-break hyphenation and flattened a 6-column table to
# 2 columns on a LaTeX report — the very pages page-patching exists to repair.
OCR_MODES = ("ocr", "auto", "txt")
DEFAULT_OCR_MODE = "ocr"
# Time budget for the driver subprocess. standard runs ~2.5–3.5 s/page on
# Apple Silicon plus ~10 s of model setup — a 500-page document fits inside
# 60 minutes with a wide margin.
MINERU_TIMEOUT_SECONDS = 3600

INSTALL_HINT = (
    "MinerU tier not installed. Install the opt-in tier (its own venv, "
    "<state>/mineru/venv, MinerU 4 + ~2 GB of models):\n"
    "    python3 <skill_dir>/scripts/ensure_env.py --tier mineru\n"
    "Or point --mineru-python at a Python that has mineru>=4.2 installed."
)


def _resolve_tier(tier: str | None, backend: str | None,
                  warnings: list[str]) -> str:
    """--tier wins; a legacy --backend maps onto a tier with a note."""
    if tier:
        resolved = tier
    elif backend:
        resolved = _LEGACY_BACKEND_TIERS[backend]
        warnings.append(
            f"--backend {backend} is a MinerU 3.x name; ran MinerU 4 tier "
            f"'{resolved}'")
    else:
        resolved = DEFAULT_TIER
    if resolved == "basic":
        warnings.append(
            "MinerU 4 'basic' tier has no Cyrillic OCR: Russian text comes out "
            "empty. Use --tier standard for anything non-Latin.")
    return resolved


def _check_pages(src: Path, pages: list[int]) -> None:
    """Fail early with a clear message when --pages names pages the PDF
    does not have (MinerU's own page_range error is less specific)."""
    import pymupdf  # type: ignore — provided by the lightweight tier

    with pymupdf.open(str(src)) as doc:
        page_count = doc.page_count
    invalid = [p for p in pages if p < 1 or p > page_count]
    if invalid:
        raise ValueError(
            f"requested pages out of range for {src.name} "
            f"(1..{page_count}): {invalid}"
        )


def _mineru_python(override: str | None = None,
                   legacy_bin: str | None = None) -> tuple[str, str | None] | None:
    """Resolve (python, models_dir) for the driver: an explicit
    `--mineru-python`, else the sibling `python` of a legacy `--mineru-bin`,
    else the `--tier mineru` venv (<state>/mineru/venv) with its models dir
    (<state>/mineru/models). Returns None when none is usable.

    A user-supplied interpreter keeps MinerU's own model location (~/.mineru)
    — models_dir is only pinned for the tier venv this skill manages.
    """
    if legacy_bin and not override:
        b = Path(legacy_bin).expanduser()
        override = str(b.with_name("python.exe" if os.name == "nt" else "python"))
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate), None
        return None
    try:
        import ensure_env  # noqa: E402 — stdlib-only, next to this script
        if ensure_env.isolated_ready("mineru"):
            return (str(ensure_env.isolated_python("mineru")),
                    str(ensure_env.isolated_models_dir("mineru")))
    except Exception:  # noqa: BLE001 — treat as "not installed"
        pass
    return None


def _run_driver(python: str, input_pdf: Path, out_dir: Path, tier: str,
                ocr_mode: str, pages: list[int] | None,
                models_dir: str | None) -> dict:
    """Run `_mineru_driver.py parse` and return its JSON line. Raises
    RuntimeError on a non-zero exit, a timeout, or an unreadable reply.

    MinerU logs progress on stderr; it is captured and its tail surfaced on
    failure. `-I` keeps this scripts dir off the tier venv's sys.path.
    """
    cmd = [python, "-I", str(DRIVER), "parse", str(input_pdf),
           "--out", str(out_dir), "--tier", tier, "--ocr-mode", ocr_mode]
    if pages:
        cmd += ["--pages", format_page_list(pages).replace(" ", "")]
    if models_dir:
        cmd += ["--models-dir", models_dir]
    log(f"$ {' '.join(cmd)}", prefix="mineru")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=MINERU_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"mineru exceeded the {MINERU_TIMEOUT_SECONDS}-second timeout") from e
    reply: dict = {}
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith("{"):
            try:
                reply = json.loads(line)
            except json.JSONDecodeError:
                continue
            break
    if proc.returncode != 0 or not reply.get("ok"):
        tail = "\n".join((proc.stderr or "").splitlines()[-12:])
        reason = reply.get("error") or tail or "no output captured"
        raise RuntimeError(f"mineru driver exited with code {proc.returncode}: {reason}")
    return reply


def _locate_outputs(out_dir: Path) -> dict[str, Path]:
    """MinerU 4's saved result: `markdown.md`, `middle_json.json`,
    `structured_content.json`, `images/`, plus the driver's
    `content_list.json`. Raises RuntimeError when the markdown is missing."""
    md = out_dir / "markdown.md"
    if not md.is_file():
        raise RuntimeError(
            f"mineru produced no markdown; output tree was "
            f"{sorted(p.name for p in out_dir.iterdir()) if out_dir.exists() else 'missing'}"
        )
    found: dict[str, Path] = {"root": out_dir, "markdown": md}
    for key, name in (("content_list", "content_list.json"),
                      ("middle", "middle_json.json"), ("images", "images")):
        p = out_dir / name
        if p.exists():
            found[key] = p
    return found


# MinerU renders embedded images as Markdown links (`![](images/<name>.jpg)`)
# or, inside tables, as HTML <img> tags. We catch both so the asset-rewrite
# pass captures every reference.
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(images/([^)\s]+)\)")
_HTML_IMG_RE = re.compile(r'<img\s+[^>]*src=["\']images/([^"\']+)["\']', re.IGNORECASE)
# MinerU 4 names materialized assets `page_<page_idx>_<kind>_<n>.<ext>`.
_ASSET_PAGE_RE = re.compile(r"^page_(\d+)_")


def _copy_images_and_rewrite(
    body: str,
    images_src: Path | None,
    doc_id: str,
    assets_dir: Path,
    assets_rel_prefix: str,
    content_list: list[dict],
    page_patch: bool = False,
    asset_suffix: str = "",
) -> tuple[str, list[str], list[str]]:
    """Copy every image the markdown body links into the kb assets dir and
    rewrite the links to the new filenames.

    Numbering follows page order (`<doc_id>-pageNN-imgM.<ext>`) so the
    asset names sort alongside the page anchors and match the scheme used
    by extract_pdf_pymupdf4llm.py — second-session agents see one
    consistent naming convention regardless of which backend ran. Page
    numbers come from content_list `page_idx` (original numbering, also
    under --pages), falling back to MinerU's `page_<idx>_` filename prefix.

    Only images the body links are copied: MinerU 4's images/ also holds
    crops of every equation and table, which the body already carries as
    LaTeX / HTML — copying them would leak ghost assets into the kb.

    `page_patch` pads page numbers to 3 digits so successive patch runs on
    the same document keep stable filenames (patching [28] today and
    [28, 142] tomorrow both write `doc-017-page028-mineru-imgM.ext`).
    Full-document mode keeps the legacy 2-digit padding. `asset_suffix`
    (e.g. `"-mineru"`) is inserted between the page token and `-img<n>`.

    Returns (rewritten_body, asset_relpaths, warnings).
    """
    warnings: list[str] = []
    if images_src is None or not images_src.is_dir():
        return body, [], warnings

    linked = {Path(m).name for m in _MD_IMAGE_RE.findall(body)}
    linked |= {Path(m).name for m in _HTML_IMG_RE.findall(body)}
    if not linked:
        return body, [], warnings

    page_of: dict[str, int] = {}
    order: list[str] = []
    for entry in content_list:
        if not isinstance(entry, dict):
            continue
        img_path = entry.get("img_path")
        if not isinstance(img_path, str) or not img_path:
            continue
        basename = Path(img_path).name
        if basename in page_of or basename not in linked:
            continue
        page_idx = entry.get("page_idx")
        page_of[basename] = int(page_idx) + 1 if isinstance(page_idx, int) else 0
        order.append(basename)
    for basename in sorted(linked - set(page_of)):
        m = _ASSET_PAGE_RE.match(basename)
        page_of[basename] = int(m.group(1)) + 1 if m else 0
        order.append(basename)

    try:
        assets_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        warnings.append(f"cannot create assets dir {assets_dir}: {e}")
        return body, [], warnings

    page_pad = 3 if page_patch else 2
    per_page_counter: dict[int, int] = {}
    rename_map: dict[str, str] = {}
    saved_relpaths: list[str] = []
    dedup_cache: dict[str, Path] = {}
    for basename in sorted(order, key=lambda b: page_of[b]):
        src = images_src / basename
        if not src.is_file():
            warnings.append(f"image {basename} is linked but missing from mineru output")
            continue
        try:
            blob = src.read_bytes()
        except OSError as e:
            warnings.append(f"failed to read {basename}: {e}")
            continue
        page_no = page_of[basename]
        ordinal = per_page_counter.get(page_no, 0) + 1
        per_page_counter[page_no] = ordinal
        ext = src.suffix.lower().lstrip(".") or "jpg"
        target = assets_dir / (
            f"{doc_id}-page{page_no:0{page_pad}d}{asset_suffix}-img{ordinal}.{ext}")
        result = save_image_safe(blob, target, dedup_cache=dedup_cache, source_ext=ext)
        if not result:
            note = skipped_image_note(result, ext)
            if note:
                warnings.append(f"image {basename}: {note}")
            continue
        rel = f"{assets_rel_prefix}/{result.path.name}"
        rename_map[basename] = rel
        if rel not in saved_relpaths:
            saved_relpaths.append(rel)

    if not rename_map:
        return body, saved_relpaths, warnings

    def _rewrite_md(match: re.Match[str]) -> str:
        new_rel = rename_map.get(Path(match.group(1)).name)
        if new_rel is None:
            return match.group(0)
        alt = match.group(0).split("![", 1)[1].split("]", 1)[0]
        return f"![{alt}]({new_rel})"

    def _rewrite_html(match: re.Match[str]) -> str:
        new_rel = rename_map.get(Path(match.group(1)).name)
        if new_rel is None:
            return match.group(0)
        return match.group(0).replace(f"images/{match.group(1)}", new_rel)

    new_body = _MD_IMAGE_RE.sub(_rewrite_md, body)
    new_body = _HTML_IMG_RE.sub(_rewrite_html, new_body)
    return new_body, saved_relpaths, warnings


_ANCHOR_SKIP_TYPES = {"header", "footer", "page_number", "aside_text", "page_footnote"}
# Markdown block prefixes an anchor may sit behind: "## ", "- ", "> ", "1. ".
_LINE_PREFIX_RE = re.compile(r"[#>*+\-\s]*|\s*\d+[.)]\s*")


def _find_anchor(body: str, entry: dict, cursor: int) -> int:
    """Body offset where `entry` starts (searching from `cursor`), or -1.

    Text, titles and equations match on their own text; images, charts and
    tables on the line that links their `images/<name>` or, failing that,
    on their caption. Snippets shorter than 6 characters are too ambiguous
    to anchor on."""
    snippets: list[str] = []
    text = entry.get("text")
    if entry.get("type") in ("text", "title", "equation") and isinstance(text, str):
        snippets.append(text.strip())
    img_path = entry.get("img_path")
    if isinstance(img_path, str) and img_path.startswith("images/"):
        for needle in (f"]({img_path})", f'src="{img_path}"'):
            pos = body.find(needle, cursor)
            if pos >= 0:
                return body.rfind("\n", 0, pos) + 1
    for key in ("image_caption", "table_caption"):
        captions = entry.get(key)
        if isinstance(captions, list):
            snippets += [c.strip() for c in captions if isinstance(c, str)]
    for snippet in snippets:
        if len(snippet) < 6:
            continue
        snippet = snippet[:120]
        idx = body.find(snippet, cursor)
        if idx < 0:
            idx = body.find(re.sub(r"\s+", " ", snippet), cursor)
        if idx >= 0:
            # Start the page at the line, not after its "## " / "- " / "$$"
            # prefix, or the previous page would end with a dangling marker.
            line_start = body.rfind("\n", 0, idx) + 1
            if line_start >= cursor and _LINE_PREFIX_RE.fullmatch(body[line_start:idx]):
                idx = line_start
            if (entry.get("type") == "equation" and idx - 3 >= cursor
                    and body.startswith("$$", idx - 3)):
                idx = body.rfind("\n", 0, idx - 3) + 1
            return idx
    return -1


def _inject_page_anchors(body: str, content_list: list[dict]) -> str:
    """Insert `[page N]` markers into the body using the content_list as
    the authoritative page ordering. MinerU's markdown is already laid out
    in reading order but lacks per-page separators; we splice them in by
    matching each page's first locatable entry (text, heading, equation,
    linked image, caption) against the body.

    The implementation is conservative: when alignment fails we leave the
    body unchanged rather than fabricating page boundaries — losing the
    anchors is preferable to mis-locating them, since second-session
    agents cite page numbers from these markers. `page_idx` is MinerU's
    original 0-based page index (also under --pages), so N is always the
    page number in the source PDF.
    """
    if not content_list:
        return body
    pieces: list[str] = []
    cursor = 0
    rendered_pages: set[int] = set()
    for entry in content_list:
        if not isinstance(entry, dict) or entry.get("type") in _ANCHOR_SKIP_TYPES:
            continue
        page_idx = entry.get("page_idx")
        if not isinstance(page_idx, int):
            continue
        page_no = page_idx + 1
        if page_no in rendered_pages:
            continue
        idx = _find_anchor(body, entry, cursor)
        if idx < 0:
            continue
        pieces.append(body[cursor:idx])
        pieces.append(f"\n\n[page {page_no}]\n\n")
        cursor = idx
        rendered_pages.add(page_no)
    pieces.append(body[cursor:])
    rebuilt = "".join(pieces).strip() + "\n"
    return re.sub(r"\n{3,}", "\n\n", rebuilt)


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


def _cache_raw_mineru_output(located: dict, cache_dir: Path, doc_id: str) -> Path | None:
    """Preserve MinerU's output under `<kb_dir>/_mineru/<doc_id>/vlm/` for a
    later opt-in `postprocess_popo.py` run, plus the Content List V1 shim
    `<doc_id>_content_list.json` that MinerU-Popo's MinerU reader looks for
    (`<input>/<doc_id>/vlm/<doc_id>_{model,middle,content_list}.json`).
    Returns the cache dir on success, None on failure (non-fatal — Popo just
    won't be available for that doc)."""
    target = cache_dir / doc_id
    inner = target / "vlm"
    try:
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(located["root"], inner)
        if "content_list" in located:
            shutil.copyfile(located["content_list"], inner / f"{doc_id}_content_list.json")
    except OSError as e:
        log(f"failed to cache raw mineru output to {target}: {e}", prefix="mineru")
        return None
    return target


def _read_content_list(located: dict, warnings: list[str]) -> list:
    """Parse content_list.json. Returns an empty list (with warning) on any
    decode/IO failure — callers proceed without page anchors."""
    path = located.get("content_list")
    if path is None:
        warnings.append("content_list.json missing — page anchors skipped")
        return []
    try:
        content_list = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        warnings.append(f"content_list.json unreadable: {e}")
        return []
    if not isinstance(content_list, list):
        warnings.append("content_list.json was not a list — page anchors skipped")
        return []
    return content_list


def _compute_page_count(content_list: list, pages: list[int] | None) -> int | None:
    """With --pages, "pages" is the size of the patched subset; otherwise
    fall back to the max page_idx+1 from content_list."""
    if pages:
        return len(pages)
    max_page = 0
    for entry in content_list:
        if isinstance(entry, dict) and isinstance(entry.get("page_idx"), int):
            max_page = max(max_page, entry["page_idx"] + 1)
    return max_page or None


def extract(
    input_pdf: Path,
    output_md: Path,
    doc_id: str,
    tier: str,
    ocr_mode: str,
    assets_dir: Path,
    assets_rel_prefix: str,
    mineru_cache_dir: Path | None,
    mineru_python: str | None,
    keep_raw: bool,
    pages: list[int] | None = None,
    asset_suffix: str = "",
    mineru_bin: str | None = None,
    warnings: list[str] | None = None,
) -> tuple[str, dict, list[str], dict]:
    """Returns (body, frontmatter_extras, warnings, audit_extras).

    When `pages` is non-empty only those pages are parsed (MinerU's native
    page_range); anchors and asset filenames carry the original page numbers.
    """
    warnings = list(warnings or [])
    extras: dict = {}
    audit: dict = {"mineru_tier": tier, "mineru_ocr_mode": ocr_mode}

    resolved = _mineru_python(mineru_python, mineru_bin)
    if resolved is None:
        raise FileNotFoundError(INSTALL_HINT)
    python, models_dir = resolved

    if pages:
        try:
            _check_pages(input_pdf, pages)
        except ValueError as e:
            raise RuntimeError(f"--pages: {e}") from e
        audit["page_subset_requested"] = list(pages)
        audit["page_subset_size"] = len(pages)

    with tempfile.TemporaryDirectory(prefix="mineru-") as tmp:
        out_dir = Path(tmp) / "out"
        reply = _run_driver(python, input_pdf, out_dir, tier, ocr_mode, pages, models_dir)
        audit["mineru_version"] = reply.get("mineru_version") or "unknown"
        located = _locate_outputs(out_dir)

        body = located["markdown"].read_text(encoding="utf-8")
        content_list = _read_content_list(located, warnings)
        body = _inject_page_anchors(body, content_list)

        body, asset_relpaths, image_warnings = _copy_images_and_rewrite(
            body=body,
            images_src=located.get("images"),
            doc_id=doc_id,
            assets_dir=assets_dir,
            assets_rel_prefix=assets_rel_prefix,
            content_list=content_list,
            page_patch=bool(pages),
            asset_suffix=asset_suffix,
        )
        warnings.extend(image_warnings)
        if asset_relpaths:
            extras["assets"] = asset_relpaths

        extras["headings"] = _extract_headings_from_content_list(content_list)
        pages_count = _compute_page_count(content_list, pages)
        if pages_count:
            extras["pages"] = pages_count

        if keep_raw and mineru_cache_dir is not None:
            cached = _cache_raw_mineru_output(located, mineru_cache_dir, doc_id)
            if cached is not None:
                audit["mineru_raw_cache"] = str(cached)
            else:
                warnings.append(
                    f"could not preserve raw mineru output for postprocess "
                    f"(--keep-raw requested but copy to {mineru_cache_dir} failed)")

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
        description="Extract PDF → Markdown via the opt-in MinerU 4 tier.",
    )
    ap.add_argument("input_pdf")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    ap.add_argument(
        "--tier",
        default=None,
        choices=SUPPORTED_TIERS,
        help=f"MinerU 4 quality tier (default: {DEFAULT_TIER}). 'basic' has "
             "no Cyrillic OCR — Russian text comes out empty.",
    )
    ap.add_argument(
        "--ocr-mode",
        default=DEFAULT_OCR_MODE,
        choices=OCR_MODES,
        help=f"MinerU OCR mode (default: {DEFAULT_OCR_MODE}: the VLM reads every "
             "block; 'auto' trusts the PDF text layer where present).",
    )
    ap.add_argument(
        "--backend",
        default=None,
        choices=SUPPORTED_BACKENDS,
        help="Legacy MinerU 3.x backend name; every value runs the standard tier.",
    )
    ap.add_argument(
        "--lang",
        default=None,
        help="Legacy MinerU 3.x OCR language; MinerU 4 has no such setting, "
             "the value is ignored with a warning.",
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
        "--mineru-python",
        default=None,
        help="Python interpreter that has mineru>=4.2 installed (default: the "
             "<state>/mineru/venv built by ensure_env.py --tier mineru).",
    )
    ap.add_argument(
        "--mineru-bin",
        default=None,
        help="Legacy: path to a `mineru` executable; the `python` next to it "
             "is used as --mineru-python.",
    )
    ap.add_argument(
        "--keep-raw",
        action="store_true",
        help="Preserve MinerU's output under the cache dir (plus the "
             "<doc_id>_content_list.json shim) for postprocess_popo.py.",
    )
    ap.add_argument(
        "--pages",
        default=None,
        help="Page-range patching: comma-separated list of 1-based page "
             "numbers with optional ranges, e.g. '2,18-19,35,221'. Only "
             "those pages are parsed (MinerU's native page_range); the "
             "`[page N]` anchors and asset filenames keep the original page "
             "numbers. Use this to recover vector math/diagrams that "
             "pymupdf4llm dropped on specific pages without re-extracting "
             "the whole document.",
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
    """Resolve CLI args to absolute paths + parsed --pages list + tier.
    Raises _ArgsError when the CLI is in a bad shape."""
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

    warnings: list[str] = []
    tier = _resolve_tier(args.tier, args.backend, warnings)
    if args.lang:
        warnings.append(
            f"--lang {args.lang} ignored: MinerU 4 has no OCR language setting "
            "(the standard tier's VLM reads Cyrillic as is)")

    return {
        "in_path": in_path,
        "out_path": out_path,
        "source_rel": source_rel,
        "pages": pages,
        "assets_dir": assets_dir,
        "cache_dir": cache_dir,
        "asset_suffix": asset_suffix,
        "tier": tier,
        "warnings": warnings,
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
            tier=resolved["tier"],
            ocr_mode=args.ocr_mode,
            assets_dir=resolved["assets_dir"],
            assets_rel_prefix=args.assets_rel,
            mineru_cache_dir=resolved["cache_dir"],
            mineru_python=args.mineru_python,
            keep_raw=args.keep_raw,
            pages=resolved["pages"],
            asset_suffix=resolved["asset_suffix"],
            mineru_bin=args.mineru_bin,
            warnings=resolved["warnings"],
        )
    except FileNotFoundError as e:
        # mineru tier missing — exit 2 so the parent loop can mark the
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

    extraction_method = (
        f"{EXTRACTOR_NAME}-{resolved['tier']}@{audit.get('mineru_version', 'unknown')}"
    )
    input_sha256 = sha256_of(resolved["in_path"])

    if args.patch_into:
        return _run_patch_mode(args, resolved, body, extras, warnings, audit,
                               extraction_method, input_sha256)
    return _run_standalone_mode(args, resolved, body, extras, warnings, audit,
                                extraction_method, input_sha256)


if __name__ == "__main__":
    sys.exit(main())
