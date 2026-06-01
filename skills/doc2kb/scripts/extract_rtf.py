#!/usr/bin/env python3
"""
extract_rtf.py — Phase 4 extractor for Rich Text Format (.rtf) files.

CLI:
    extract_rtf.py <input_rtf> <output_md>
                   [--doc-id doc-NNN]
                   [--source-rel relative/path/in/corpus.rtf]
                   [--assets-dir <abs_path>]
                   [--assets-rel <rel_prefix_from_md>]
                   [--no-extract-images]
                   [--force-striprtf]

Effect:
    Writes <output_md> with YAML frontmatter + Markdown body. Stdout receives
    a single JSON line summarizing the result.

Routing:
    - If `pandoc` is on PATH (and `--force-striprtf` is not set), the body is
      produced by pandoc (`-f rtf -t markdown`). Pandoc preserves headings,
      bold/italic, lists, tables, and embedded images (extracted to
      `<kb_dir>/assets/` via the shared `pandoc_to_markdown` helper).
    - Otherwise the body is produced by `striprtf` — a pure-Python, dependency
      -free RTF→plain-text converter from the lightweight tier. It always
      works (no system binary), but flattens structure and drops images, so a
      warning recommends installing pandoc for richer output.
    - `--force-striprtf` pins the pure-Python route even when pandoc exists
      (regression-test / no-subprocess escape hatch).

RTF is plain ASCII markup with `\\'xx` codepage escapes and `\\uN` unicode
escapes; striprtf decodes both. We therefore read the source as latin-1 (a
lossless byte→codepoint map) so the raw bytes reach striprtf intact and it can
apply the document's declared `\\ansicpg` codepage itself.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import (  # noqa: E402
    add_assets_args,
    clean_whitespace,
    count_tokens,
    emit_failure,
    emit_success,
    pandoc_available,
    pandoc_to_markdown,
    resolve_assets_dir,
    sanitize_heading,
    sha256_of,
    today_iso,
    tool_version_string,
    validate_source_rel,
    write_md,
)


EXTRACTOR_PANDOC = "pandoc"
EXTRACTOR_STRIPRTF = "striprtf"
# Below this body length on a non-empty rtf, flag a warning.
MIN_BODY_CHARS = 50


def _extract_via_striprtf(input_rtf: Path) -> tuple[str, list[str]]:
    """Pure-Python fallback. Returns (body, warnings). Loses tables/images
    and most structure — striprtf emits plain text — but never needs a
    system binary, so the lightweight tier always has a working RTF path."""
    warnings: list[str] = []
    try:
        from striprtf.striprtf import rtf_to_text  # type: ignore
    except Exception as e:
        raise RuntimeError(f"striprtf unavailable: {e}") from e
    # latin-1 is a 1:1 byte→codepoint map: no decode error, raw bytes survive
    # so striprtf can apply the document's own \ansicpg / \'xx escapes.
    raw = input_rtf.read_text(encoding="latin-1")
    try:
        text = rtf_to_text(raw, errors="ignore")
    except Exception as e:
        raise RuntimeError(f"striprtf conversion failed: {e}") from e
    return clean_whitespace(text), warnings


def extract(input_rtf: Path,
            doc_id: str = "doc-000",
            assets_dir: Path | None = None,
            assets_rel_prefix: str = "../assets",
            extract_images: bool = True,
            force_striprtf: bool = False) -> tuple[str, dict, list[str], str]:
    """Returns (markdown_body, frontmatter_extras, warnings, extractor_name).

    Prefers pandoc (structure + images) and falls back to striprtf
    (plain text, always available). See the module docstring for routing."""
    warnings: list[str] = []
    assets: list[str] = []
    use_pandoc = pandoc_available() and not force_striprtf

    if use_pandoc:
        try:
            body, w, a = pandoc_to_markdown(
                input_rtf,
                from_format="rtf",
                doc_id=doc_id,
                assets_dir=assets_dir,
                assets_rel_prefix=assets_rel_prefix,
                extract_images=extract_images,
            )
            warnings.extend(w)
            assets.extend(a)
            extractor_name = EXTRACTOR_PANDOC
        except RuntimeError as e:
            # Pandoc failure → fall back to striprtf with a loud warning so
            # the operator knows structure/images were lost.
            warnings.append(
                f"pandoc route failed ({e}); falling back to striprtf — "
                "tables, images and most structure will be flattened to text"
            )
            body, w = _extract_via_striprtf(input_rtf)
            warnings.extend(w)
            extractor_name = EXTRACTOR_STRIPRTF
    else:
        body, w = _extract_via_striprtf(input_rtf)
        warnings.extend(w)
        extractor_name = EXTRACTOR_STRIPRTF
        if not force_striprtf:
            warnings.append(
                "pandoc not on PATH — extracted via striprtf (plain text). "
                "Install pandoc (`brew install pandoc` / `apt install pandoc`) "
                "and re-run to preserve tables, images, and headings"
            )

    if len(body.strip()) < MIN_BODY_CHARS:
        warnings.append(f"extracted body is unusually short ({len(body)} chars)")

    headings = []
    for line in body.split("\n"):
        if line.startswith("# ") and len(line) < 200:
            sanitized = sanitize_heading(line[2:])
            if sanitized:
                headings.append(sanitized)
            if len(headings) >= 10:
                break
    extras: dict = {"headings": headings}
    if assets:
        extras["assets"] = assets
    return body, extras, warnings, extractor_name


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extract RTF → Markdown. Routes through pandoc when "
                    "available; otherwise via the pure-Python striprtf fallback."
    )
    ap.add_argument("input_rtf")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    add_assets_args(ap)
    ap.add_argument("--force-striprtf", action="store_true",
                    help="Bypass the pandoc route even when pandoc is on PATH.")
    args = ap.parse_args()

    in_path = Path(args.input_rtf).expanduser().resolve()
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

    assets_dir: Path | None = resolve_assets_dir(args, out_path)

    try:
        body, extras, warnings, extractor_name = extract(
            in_path,
            doc_id=args.doc_id,
            assets_dir=assets_dir,
            assets_rel_prefix=args.assets_rel,
            extract_images=not args.no_extract_images,
            force_striprtf=args.force_striprtf,
        )
    except Exception as e:
        emit_failure(f"extraction failed: {e}", extra={"input": str(in_path)})
        return 1

    fm = {
        "id": args.doc_id,
        "source": source_rel,
        "source_type": "rtf",
        "source_sha256": sha256_of(in_path),
        "extraction_method": f"{extractor_name}@{tool_version_string()}",
        "extraction_date": today_iso(),
        "headings": extras.get("headings", []),
        "tokens_estimated": count_tokens(body),
        "warnings": warnings,
    }
    if extras.get("assets"):
        fm["assets"] = extras["assets"]
    write_md(out_path, fm, body)
    emit_success(out_path, body, warnings, extra={
        "extractor": extractor_name,
        "assets_extracted": len(extras.get("assets", [])),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
