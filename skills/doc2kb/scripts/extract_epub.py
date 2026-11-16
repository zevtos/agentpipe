#!/usr/bin/env python3
"""
extract_epub.py — Phase 4 extractor for EPUB (.epub) e-books.

CLI:
    extract_epub.py <input_epub> <output_md>
                    [--doc-id doc-NNN]
                    [--source-rel relative/path/in/corpus.epub]
                    [--assets-dir <abs_path>]
                    [--assets-rel <rel_prefix_from_md>]
                    [--no-extract-images]
                    [--force-fallback]

Effect:
    Writes <output_md> with YAML frontmatter + Markdown body. Stdout receives
    a single JSON line summarizing the result.

Routing (mirrors extract_rtf.py):
    - If `pandoc` is on PATH (and `--force-fallback` is not set), the body is
      produced by pandoc (`-f epub -t markdown`). Pandoc honours the OPF spine
      reading order, preserves headings/lists/tables, converts EPUB3 MathML to
      `$...$` LaTeX, and extracts embedded images to `<kb_dir>/assets/` via the
      shared `pandoc_to_markdown` helper. This is the preferred route.
    - Otherwise a pure-Python fallback parses the EPUB as a ZIP, walks the OPF
      spine in reading order, and runs each XHTML chapter through `markdownify`
      (lightweight tier). It always works without a system binary, but flattens
      embedded images to `![image N]()` placeholders and drops MathML, so a
      warning recommends installing pandoc for richer output.
    - `--force-fallback` pins the pure-Python route even when pandoc exists
      (regression-test / no-subprocess escape hatch).

EPUB content is overwhelmingly UTF-8 XHTML; the fallback decodes with
`errors="replace"` and honours a leading UTF-16 BOM, so a stray non-UTF-8
chapter degrades gracefully instead of failing the whole book.
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

from _common import (  # noqa: E402
    add_assets_args,
    clean_whitespace,
    count_tokens,
    emit_failure,
    emit_success,
    pandoc_available,
    pandoc_to_markdown,
    parse_epub_structure,
    resolve_assets_dir,
    sanitize_heading,
    sha256_of,
    today_iso,
    tool_version_string,
    validate_source_rel,
    write_md,
)


EXTRACTOR_PANDOC = "pandoc"
EXTRACTOR_FALLBACK = "zip+markdownify"
# Below this body length on a non-empty epub, flag a warning.
MIN_BODY_CHARS = 50

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_SVG_BLOCK_RE = re.compile(r"<svg\b.*?</svg>", re.IGNORECASE | re.DOTALL)
_MATH_HINT_RE = re.compile(r"<math\b|MathML", re.IGNORECASE)
_BODY_RE = re.compile(r"<body\b[^>]*>(.*)</body>", re.IGNORECASE | re.DOTALL)
# XML/HTML prologue noise markdownify would otherwise render as literal text:
# `<?xml ...?>` declarations, other processing instructions, and `<!DOCTYPE>`.
_PROLOGUE_RE = re.compile(r"<\?.*?\?>|<!DOCTYPE[^>]*>", re.IGNORECASE | re.DOTALL)


def _decode_xhtml(raw: bytes) -> str:
    """Decode an XHTML chapter. EPUB XHTML is almost always UTF-8; honour a
    UTF-16 BOM and otherwise replace undecodable bytes rather than fail."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8", errors="replace")


def _chapter_html(html: str) -> str:
    """Reduce an XHTML document to its renderable inner content: strip the XML
    declaration / DOCTYPE and isolate the <body>, so the <head>/<title> and the
    `<?xml ...?>` prologue don't leak into the Markdown as literal text."""
    html = _PROLOGUE_RE.sub("", html)
    m = _BODY_RE.search(html)
    return m.group(1) if m else html


def _placeholder_images(html: str, counter: list[int]) -> str:
    """Replace <img>/<svg> blocks with `![image N]()` placeholders before
    markdownify, matching the no-src convention of the other extractors —
    base64/internal-zip image refs must never reach the kb Markdown."""
    def repl(_m):
        counter[0] += 1
        return f"\n\n![image {counter[0]}]()\n\n"
    html = _SVG_BLOCK_RE.sub(repl, html)
    html = _IMG_TAG_RE.sub(repl, html)
    return html


def _extract_via_zip(input_epub: Path) -> tuple[str, list[str]]:
    """Pure-Python fallback. Returns (body, warnings). Walks the OPF spine in
    reading order and markdownifies each XHTML chapter. Loses images (→
    placeholders) and MathML, but needs no system binary."""
    warnings: list[str] = []
    try:
        from markdownify import markdownify as md_from_html  # type: ignore
    except Exception as e:
        raise RuntimeError(f"markdownify unavailable: {e}") from e

    try:
        zf = zipfile.ZipFile(input_epub)
    except (zipfile.BadZipFile, OSError) as e:
        raise RuntimeError(f"not a readable EPUB/ZIP: {e}") from e

    with zf:
        try:
            struct = parse_epub_structure(zf)
        except Exception as e:
            raise RuntimeError(f"EPUB structure parse failed: {e}") from e

        spine = struct["spine"]
        if not spine:
            raise RuntimeError("EPUB spine is empty — no reading order to extract")

        names = set(zf.namelist())
        img_counter = [0]
        math_seen = False
        chapters: list[str] = []
        for href in spine:
            if href not in names:
                warnings.append(f"spine item not found in archive, skipped: {href}")
                continue
            try:
                raw = zf.read(href)
            except (KeyError, zipfile.BadZipFile) as e:
                warnings.append(f"cannot read spine item {href}: {e}")
                continue
            html = _decode_xhtml(raw)
            if not math_seen and _MATH_HINT_RE.search(html):
                math_seen = True
            html = _chapter_html(html)
            html = _placeholder_images(html, img_counter)
            try:
                chunk = md_from_html(html, heading_style="ATX",
                                     strip=["script", "style"])
            except Exception as e:
                warnings.append(f"markdownify failed on {href}: {e}")
                continue
            chunk = chunk.strip()
            if chunk:
                chapters.append(chunk)

    if img_counter[0]:
        warnings.append(
            f"{img_counter[0]} embedded image(s) replaced with placeholders — "
            "install pandoc (`brew install pandoc` / `apt install pandoc`) and "
            "re-run to extract images into the kb assets dir"
        )
    if math_seen:
        warnings.append(
            "MathML detected but the pure-Python fallback drops equations — "
            "install pandoc and re-run to preserve math as $...$ LaTeX"
        )
    body = clean_whitespace("\n\n".join(chapters))
    return body, warnings


def extract(input_epub: Path,
            doc_id: str = "doc-000",
            assets_dir: Path | None = None,
            assets_rel_prefix: str = "../assets",
            extract_images: bool = True,
            force_fallback: bool = False) -> tuple[str, dict, list[str], str]:
    """Returns (markdown_body, frontmatter_extras, warnings, extractor_name).

    Prefers pandoc (spine order + images + math) and falls back to the
    pure-Python zip+markdownify route (always available). See the module
    docstring for routing."""
    warnings: list[str] = []
    assets: list[str] = []
    extras: dict = {}

    # Read OPF metadata up front (cheap) so the book title/authors land in
    # frontmatter regardless of which body route runs. Never fatal.
    try:
        with zipfile.ZipFile(input_epub) as zf:
            struct = parse_epub_structure(zf)
        if struct.get("title"):
            extras["book_title"] = struct["title"]
        if struct.get("authors"):
            extras["book_authors"] = struct["authors"]
    except Exception:
        pass

    use_pandoc = pandoc_available() and not force_fallback
    if use_pandoc:
        try:
            body, w, a = pandoc_to_markdown(
                input_epub,
                from_format="epub",
                doc_id=doc_id,
                assets_dir=assets_dir,
                assets_rel_prefix=assets_rel_prefix,
                extract_images=extract_images,
            )
            warnings.extend(w)
            assets.extend(a)
            extractor_name = EXTRACTOR_PANDOC
        except RuntimeError as e:
            warnings.append(
                f"pandoc route failed ({e}); falling back to zip+markdownify — "
                "images become placeholders and MathML is dropped"
            )
            body, w = _extract_via_zip(input_epub)
            warnings.extend(w)
            extractor_name = EXTRACTOR_FALLBACK
    else:
        body, w = _extract_via_zip(input_epub)
        warnings.extend(w)
        extractor_name = EXTRACTOR_FALLBACK
        if not force_fallback:
            warnings.append(
                "pandoc not on PATH — extracted via zip+markdownify (images "
                "flattened to placeholders, MathML dropped). Install pandoc "
                "(`brew install pandoc` / `apt install pandoc`) and re-run to "
                "preserve images, tables, and equations"
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
    if assets:
        extras["assets"] = assets
    return body, extras, warnings, extractor_name


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extract EPUB → Markdown. Routes through pandoc when "
                    "available; otherwise via the pure-Python zip+markdownify "
                    "fallback."
    )
    ap.add_argument("input_epub")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    add_assets_args(ap)
    ap.add_argument("--force-fallback", action="store_true",
                    help="Bypass the pandoc route even when pandoc is on PATH.")
    args = ap.parse_args()

    in_path = Path(args.input_epub).expanduser().resolve()
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
            force_fallback=args.force_fallback,
        )
    except Exception as e:
        emit_failure(f"extraction failed: {e}", extra={"input": str(in_path)})
        return 1

    fm = {
        "id": args.doc_id,
        "source": source_rel,
        "source_type": "epub",
        "source_sha256": sha256_of(in_path),
        "extraction_method": f"{extractor_name}@{tool_version_string()}",
        "extraction_date": today_iso(),
        "headings": extras.get("headings", []),
        "tokens_estimated": count_tokens(body),
        "warnings": warnings,
    }
    if extras.get("book_title"):
        fm["book_title"] = extras["book_title"]
    if extras.get("book_authors"):
        fm["book_authors"] = extras["book_authors"]
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
