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

Prerequisites:
    The `mineru` CLI must be installed in the same venv as this script.
    Install via:
        python3 <skill_dir>/scripts/ensure_env.py --tier mineru
    If the CLI is missing the script exits 2 with that exact hint —
    the parent loop should mark the file as `skip` and surface the warning
    rather than crashing the corpus extraction.

Effect:
    1. Runs `mineru -p <input> -o <tmpdir> -b <backend> -l <lang>`.
    2. Locates `<tmpdir>/<stem>/<backend>/<stem>.md` and
       `_content_list.json` (MinerU's stable structured artefact).
    3. Splits the markdown by page_idx → injects `[page N]` anchors.
    4. Copies images from `<tmpdir>/<stem>/<backend>/images/` into
       `<kb_dir>/assets/<doc_id>-pageNN-img<n>.<ext>` via save_image_safe
       and rewrites every Markdown image link in the body to point at
       the renamed asset.
    5. Optionally preserves the raw mineru output under
       `<kb_dir>/_mineru/<doc_id>/` so a follow-up `postprocess_popo.py`
       run can consume it (Popo's `post-process/mineru/<run>/` input
       format expects exactly what MinerU emitted, so we avoid having to
       re-extract).

Frontmatter contract:
    Same shape as extract_pdf_pymupdf4llm.py — only `extraction_method`
    distinguishes the backend (`mineru-<backend>@<mineru_version>`).
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
    count_tokens,
    emit_failure,
    emit_success,
    log,
    sanitize_heading,
    save_image_safe,
    sha256_of,
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
# We default to `hybrid-auto-engine` to match MinerU's own CLI default
# (see mineru/cli/client.py — "Without method specified, hybrid-auto-engine
# will be used by default"). On Apple Silicon with the doc2kb mineru tier
# installed, hybrid routes layout detection through the lightweight
# pipeline stack and reserves the MLX VLM for the content crops that
# benefit from it — typically 2-3× faster than pure `vlm-auto-engine`
# on text-layer PDFs with equivalent extraction quality.
# `auto` is accepted as a friendly alias for users who want the legacy
# behaviour from older mineru releases.
SUPPORTED_BACKENDS = (
    "auto",
    "pipeline",
    "vlm-auto-engine",
    "hybrid-auto-engine",
)
_BACKEND_ALIASES = {"auto": "hybrid-auto-engine"}
DEFAULT_BACKEND = "hybrid-auto-engine"
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
) -> tuple[str, list[str], list[str]]:
    """Copy every image MinerU referenced into the kb assets dir and
    rewrite the body so links point at the new filenames.

    Numbering follows page order (`<doc_id>-pageNN-imgM.<ext>`) so the
    asset names sort alongside the page anchors and match the scheme used
    by extract_pdf_pymupdf4llm.py — second-session agents see one
    consistent naming convention regardless of which backend ran.

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
            f"{doc_id}-page{page_no:02d}-img{ordinal}.{ext}"
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


def _inject_page_anchors(body: str, content_list: list[dict]) -> str:
    """Insert `[page N]` markers into the body using the content_list as
    the authoritative page ordering. MinerU's markdown is already laid out
    in reading order but lacks per-page separators; we splice them in by
    matching consecutive text/heading entries against the body and tagging
    page transitions.

    The implementation is conservative: when alignment fails we leave the
    body unchanged rather than fabricating page boundaries — losing the
    anchors is preferable to mis-locating them, since second-session
    agents cite page numbers from these markers.
    """
    if not content_list:
        return body

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
        pieces.append(f"\n\n[page {page_no}]\n\n")
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
) -> tuple[str, dict, list[str], dict]:
    """Returns (body, frontmatter_extras, warnings, audit_extras)."""
    warnings: list[str] = []
    extras: dict = {}
    audit: dict = {}

    binary = _mineru_executable(mineru_bin)
    if binary is None:
        raise FileNotFoundError(INSTALL_HINT)

    audit["mineru_version"] = _mineru_version(binary)
    audit["mineru_backend_requested"] = backend

    stem = input_pdf.stem
    with tempfile.TemporaryDirectory(prefix="mineru-") as tmp:
        out_dir = Path(tmp)
        rc, _stdout, stderr = _run_mineru(binary, input_pdf, out_dir, backend, lang)
        if rc != 0:
            tail = "\n".join((stderr or "").splitlines()[-12:])
            raise RuntimeError(
                f"mineru exited with code {rc}: {tail or 'no stderr captured'}"
            )

        located = _locate_outputs(out_dir, stem, backend)
        if "markdown" not in located:
            raise RuntimeError(
                f"mineru produced no markdown for {stem}; output tree was "
                f"{[p.name for p in out_dir.iterdir()] if out_dir.exists() else 'missing'}"
            )

        backend_dir = located["resolved_backend"]
        audit["mineru_backend_resolved"] = backend_dir.name
        body = located["markdown"].read_text(encoding="utf-8")
        try:
            content_list = json.loads(located["content_list"].read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            warnings.append(f"content_list.json unreadable: {e}")
            content_list = []
        if not isinstance(content_list, list):
            warnings.append("content_list.json was not a list — page anchors skipped")
            content_list = []

        body = _inject_page_anchors(body, content_list)

        images_src = located.get("images")
        body, asset_relpaths, image_warnings = _copy_images_and_rewrite(
            body=body,
            images_src=images_src,
            doc_id=doc_id,
            assets_dir=assets_dir,
            assets_rel_prefix=assets_rel_prefix,
            content_list=content_list if isinstance(content_list, list) else [],
        )
        warnings.extend(image_warnings)
        if asset_relpaths:
            extras["assets"] = asset_relpaths

        headings = _extract_headings_from_content_list(content_list)
        extras["headings"] = headings

        # Page count from content_list (max page_idx + 1) or fall back to 0.
        max_page = 0
        for entry in content_list:
            if isinstance(entry, dict) and isinstance(entry.get("page_idx"), int):
                max_page = max(max_page, entry["page_idx"] + 1)
        if max_page:
            extras["pages"] = max_page

        if keep_raw and mineru_cache_dir is not None:
            cached = _cache_raw_mineru_output(backend_dir, mineru_cache_dir, doc_id)
            if cached is not None:
                audit["mineru_raw_cache"] = str(cached)
            else:
                warnings.append(
                    f"could not preserve raw mineru output for postprocess "
                    f"(--keep-raw requested but copy to {mineru_cache_dir} failed)"
                )

    return body, extras, warnings, audit


def main() -> int:
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

    if args.assets_dir:
        assets_dir = Path(args.assets_dir).expanduser().resolve()
    else:
        assets_dir = out_path.parent.parent / "assets"

    if args.mineru_cache_dir:
        cache_dir = Path(args.mineru_cache_dir).expanduser().resolve()
    else:
        cache_dir = out_path.parent.parent / "_mineru"

    try:
        body, extras, warnings, audit = extract(
            input_pdf=in_path,
            output_md=out_path,
            doc_id=args.doc_id,
            backend=args.backend,
            lang=args.lang,
            assets_dir=assets_dir,
            assets_rel_prefix=args.assets_rel,
            mineru_cache_dir=cache_dir,
            mineru_bin=args.mineru_bin,
            keep_raw=args.keep_raw,
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

    backend_tag = audit.get("mineru_backend_resolved") or args.backend
    fm = {
        "id": args.doc_id,
        "source": source_rel,
        "source_type": "pdf",
        "source_sha256": sha256_of(in_path),
        "extraction_method": (
            f"{EXTRACTOR_NAME}-{backend_tag}@{audit.get('mineru_version', 'unknown')}"
        ),
        "extraction_date": today_iso(),
        "pages": extras.get("pages"),
        "headings": extras.get("headings", []),
        "tokens_estimated": count_tokens(body),
        "warnings": warnings,
    }
    if extras.get("assets"):
        fm["assets"] = extras["assets"]
    write_md(out_path, fm, body)

    success_extras: dict = {
        "pages": extras.get("pages"),
        "assets_extracted": len(extras.get("assets", [])),
    }
    success_extras.update(audit)
    emit_success(out_path, body, warnings, extra=success_extras)
    return 0


if __name__ == "__main__":
    sys.exit(main())
