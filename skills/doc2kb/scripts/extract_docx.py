#!/usr/bin/env python3
"""
extract_docx.py — Phase 4 extractor for DOCX files.

CLI:
    extract_docx.py <input_docx> <output_md>
                    [--doc-id doc-NNN]
                    [--source-rel relative/path/in/corpus.docx]
                    [--force-mammoth]

Effect:
    Writes <output_md> with YAML frontmatter + Markdown body. Stdout receives
    a single JSON line summarizing the result.

Routing:
    - If the document contains OOXML math (`<m:oMath>` / `<m:oMathPara>`)
      AND `pandoc` is on PATH, the body is produced by pandoc (math survives
      as `$...$` / `$$...$$` LaTeX). Mammoth's HTML/markdown writers both
      silently drop oMath elements, which destroys the math content the
      second-session agent will be asked about.
    - Otherwise (no math, or pandoc unavailable) the body is produced by
      mammoth → HTML → markdownify, which preserves headings/lists/tables
      better than pandoc for prose-only docs.
    - `--force-mammoth` bypasses the pandoc route even when math is present
      (regression-test escape hatch).

Pipeline (mammoth route):
    1. mammoth.convert_to_html(input) — semantic conversion preserving
       headings, lists, tables, inline images, footnotes.
    2. markdownify(html) — HTML → Markdown.
    3. python-docx scout for `inline_images`, `tables`, `paragraphs`,
       `has_equations`, `has_tracked_changes` (already gathered by scout
       but we re-derive here so the extractor is callable standalone).

Why HTML→Markdown instead of mammoth.convert_to_markdown:
    mammoth's built-in markdown writer drops tables. The HTML route keeps
    them, even if mammoth's HTML for tables is plain (no header markers
    beyond <th> — but markdownify renders them as Markdown pipe tables).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from _common import (  # noqa: E402
    clean_whitespace,
    count_tokens,
    emit_failure,
    emit_success,
    sanitize_heading,
    sha256_of,
    today_iso,
    tool_version_string,
    validate_source_rel,
    write_md,
)


EXTRACTOR_MAMMOTH = "mammoth+markdownify"
EXTRACTOR_PANDOC = "pandoc"
# Below this body length on a non-empty docx, flag a warning.
MIN_BODY_CHARS = 50
# Hard cap on pandoc invocation; pandoc on a typical lab docx finishes in
# <500ms, so 60s is generous and still catches a wedged process.
PANDOC_TIMEOUT_SEC = 60


def _try_import():
    try:
        import mammoth  # type: ignore
        from markdownify import markdownify as md_from_html  # type: ignore
        from docx import Document  # type: ignore
        return mammoth, md_from_html, Document
    except Exception as e:
        emit_failure(f"required libraries unavailable: {e}")
        sys.exit(1)


def _scout_docx(path: Path) -> dict:
    """Light re-scan to populate frontmatter — duplicates scout_corpus.py
    logic but keeps the extractor invocable without scout output."""
    from docx import Document  # type: ignore
    info = {
        "paragraphs": 0,
        "inline_images": 0,
        "has_tables": False,
        "has_equations": False,
        "has_tracked_changes": False,
    }
    try:
        doc = Document(str(path))
        info["paragraphs"] = len(doc.paragraphs)
        info["inline_images"] = len(doc.inline_shapes)
        info["has_tables"] = len(doc.tables) > 0
        xml = doc.element.xml
        info["has_equations"] = "<m:oMath" in xml or "<m:oMathPara" in xml
        info["has_tracked_changes"] = "w:ins" in xml or "w:del" in xml
    except Exception:
        pass
    return info


def _extract_via_mammoth(input_docx: Path) -> tuple[str, list[str]]:
    """Mammoth → HTML → Markdown route. Returns (body, warnings)."""
    mammoth, md_from_html, _Document = _try_import()
    warnings: list[str] = []

    # Convert DOCX → HTML. By default mammoth embeds images as base64 data
    # URIs — this catastrophically inflates output (~megabytes per file with
    # diagrams) and is useless for LLM consumption. Replace each image with
    # a compact placeholder that preserves semantics (caption + count).
    img_idx = {"n": 0}

    def _image_placeholder(image):
        img_idx["n"] += 1
        alt = (image.alt_text or "").strip()
        if alt:
            label = f"image {img_idx['n']}: {alt}"
        else:
            label = f"image {img_idx['n']}"
        # Returning {"src": ""} would emit <img src="">; mammoth lets us
        # return any attribute dict. Empty src + alt is cheapest.
        return {"src": "", "alt": label[:120]}

    image_handler = mammoth.images.img_element(_image_placeholder)
    try:
        with input_docx.open("rb") as fh:
            result = mammoth.convert_to_html(fh, convert_image=image_handler)
    except Exception as e:
        raise RuntimeError(f"mammoth conversion failed: {e}") from e

    html = result.value or ""
    # mammoth's messages are tuples of (type, message); promote warnings.
    for m in result.messages or []:
        try:
            mt = m.type if hasattr(m, "type") else m[0]
            mm = m.message if hasattr(m, "message") else m[1]
        except Exception:
            mt = "warning"
            mm = str(m)
        # mammoth reports lots of style-mapping notices that are noise here.
        if mt == "warning" and "style" not in str(mm).lower():
            warnings.append(f"mammoth: {mm}"[:200])

    # HTML → Markdown. heading_style=ATX renders "# heading" instead of underline.
    md = md_from_html(html, heading_style="ATX", bullets="-",
                      strip=["style", "script"])
    body = clean_whitespace(md)
    return body, warnings


def _extract_via_pandoc(input_docx: Path) -> tuple[str, list[str]]:
    """Pandoc route — used when the source contains OOXML math that mammoth
    would silently drop. Pandoc emits math as `$...$` / `$$...$$` LaTeX,
    which the LLM can read and render in any downstream context.

    Pandoc also handles tables, lists, footnotes, and headings competently;
    the only reason we still default to mammoth for prose-only docs is that
    mammoth produces slightly cleaner list nesting and avoids pandoc's
    blockquote-wrapping of continuation paragraphs inside numbered lists.

    Returns (body, warnings)."""
    warnings: list[str] = []
    cmd = [
        "pandoc",
        "-f", "docx",
        "-t", "markdown",
        "--wrap=none",
        str(input_docx),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=PANDOC_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"pandoc timed out after {PANDOC_TIMEOUT_SEC}s")
    except FileNotFoundError:
        raise RuntimeError("pandoc binary not found on PATH")
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(
            f"pandoc exited with code {proc.returncode}: {stderr[:200]}"
        )
    body = proc.stdout.decode("utf-8", "replace")
    body = clean_whitespace(body)
    if proc.stderr:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        for ln in stderr.splitlines():
            ln = ln.strip()
            if ln and ln.lower().startswith(("warning", "[warning]")):
                warnings.append(f"pandoc: {ln}"[:200])
    return body, warnings


def extract(input_docx: Path,
            force_mammoth: bool = False) -> tuple[str, dict, list[str], str]:
    """Returns (markdown_body, frontmatter_extras, warnings, extractor_name).

    Routes math-bearing documents through pandoc (when available) so OOXML
    math survives as LaTeX instead of being silently dropped. See the
    module docstring for the full routing table."""
    extras = _scout_docx(input_docx)
    pandoc_available = shutil.which("pandoc") is not None
    use_pandoc = (
        extras.get("has_equations") and pandoc_available and not force_mammoth
    )
    warnings: list[str] = []

    if use_pandoc:
        try:
            body, w = _extract_via_pandoc(input_docx)
            warnings.extend(w)
            extractor_name = EXTRACTOR_PANDOC
        except RuntimeError as e:
            # Pandoc failure → fall back to mammoth with a loud warning so
            # the operator knows math was dropped.
            warnings.append(
                f"pandoc route failed ({e}); falling back to mammoth — "
                "OOXML math elements will be dropped from the output"
            )
            body, w = _extract_via_mammoth(input_docx)
            warnings.extend(w)
            extractor_name = EXTRACTOR_MAMMOTH
    else:
        body, w = _extract_via_mammoth(input_docx)
        warnings.extend(w)
        extractor_name = EXTRACTOR_MAMMOTH
        if extras.get("has_equations") and not pandoc_available:
            warnings.append(
                "has_equations=true but pandoc is not on PATH — OOXML math "
                "elements (m:oMath) were dropped by mammoth. Install pandoc "
                "(`brew install pandoc` / `apt install pandoc`) and re-run "
                "to preserve formulas as LaTeX"
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
    extras["headings"] = headings

    return body, extras, warnings, extractor_name


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extract DOCX → Markdown. Routes through pandoc when the "
                    "source contains OOXML math; otherwise via mammoth + markdownify."
    )
    ap.add_argument("input_docx")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    ap.add_argument("--force-mammoth", action="store_true",
                    help="Bypass the pandoc-for-math route even when math is present.")
    args = ap.parse_args()

    in_path = Path(args.input_docx).expanduser().resolve()
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
        body, extras, warnings, extractor_name = extract(
            in_path, force_mammoth=args.force_mammoth
        )
    except Exception as e:
        emit_failure(f"extraction failed: {e}", extra={"input": str(in_path)})
        return 1

    fm = {
        "id": args.doc_id,
        "source": source_rel,
        "source_type": "docx",
        "source_sha256": sha256_of(in_path),
        "extraction_method": f"{extractor_name}@{tool_version_string()}",
        "extraction_date": today_iso(),
        "paragraphs": extras.get("paragraphs", 0),
        "inline_images": extras.get("inline_images", 0),
        "has_tables": extras.get("has_tables", False),
        "has_equations": extras.get("has_equations", False),
        "has_tracked_changes": extras.get("has_tracked_changes", False),
        "headings": extras.get("headings", []),
        "tokens_estimated": count_tokens(body),
        "warnings": warnings,
    }
    write_md(out_path, fm, body)
    emit_success(out_path, body, warnings, extra={
        "inline_images": extras.get("inline_images", 0),
        "has_tables": extras.get("has_tables", False),
        "extractor": extractor_name,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
