#!/usr/bin/env python3
"""
extract_html.py — Phase 4 extractor for HTML files.

CLI:
    extract_html.py <input_html> <output_md>
                    [--doc-id doc-NNN]
                    [--source-rel rel/path.html]

Pipeline:
    1. Detect encoding via charset-normalizer.
    2. trafilatura.extract(..., output_format='markdown', include_tables=True)
       — pulls main content, drops nav/footer/ads.
    3. Fallback: if trafilatura returns nothing, markdownify the raw HTML.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import (  # noqa: E402
    clean_whitespace,
    count_tokens,
    detect_text_encoding,
    emit_failure,
    emit_success,
    sanitize_heading,
    sha256_of,
    today_iso,
    tool_version_string,
    validate_source_rel,
    write_md,
)


MIN_BODY_CHARS = 50


def _detect_encoding(path: Path) -> str:
    """BOM → UTF-8 → charset-normalizer. HTML pages mostly declare their
    encoding or are UTF-8, so we don't try legacy codec candidates."""
    return detect_text_encoding(path)


def _try_trafilatura(html_text: str) -> str | None:
    try:
        import trafilatura  # type: ignore
    except Exception:
        return None
    try:
        # output_format="markdown" yields Markdown directly.
        result = trafilatura.extract(
            html_text,
            output_format="markdown",
            include_tables=True,
            include_comments=False,
            include_formatting=True,
            with_metadata=False,
            favor_recall=True,
        )
        return result
    except Exception:
        return None


def _fallback_markdownify(html_text: str) -> str:
    try:
        from markdownify import markdownify as md_from_html  # type: ignore
    except Exception:
        return ""
    try:
        return md_from_html(html_text, heading_style="ATX",
                            strip=["script", "style"])
    except Exception:
        return ""


def extract(input_path: Path) -> tuple[str, dict, list[str], str]:
    warnings: list[str] = []
    enc = _detect_encoding(input_path)
    try:
        html_text = input_path.read_text(encoding=enc, errors="replace")
    except Exception as e:
        raise RuntimeError(f"read failed: {e}") from e

    used = "trafilatura"
    body = _try_trafilatura(html_text)
    if not body or len(body.strip()) < MIN_BODY_CHARS:
        warnings.append("trafilatura returned little/no content; falling back to markdownify")
        body = _fallback_markdownify(html_text)
        used = "markdownify"

    if not body:
        body = ""
        warnings.append("could not extract any content from HTML")

    body = clean_whitespace(body)

    extras: dict = {"source_encoding": enc}
    headings = []
    for line in body.split("\n"):
        if line.startswith("# ") and len(line) < 200:
            sanitized = sanitize_heading(line[2:])
            if sanitized:
                headings.append(sanitized)
            if len(headings) >= 10:
                break
    extras["headings"] = headings
    return body, extras, warnings, used


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract HTML → Markdown via trafilatura.")
    ap.add_argument("input_html")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    args = ap.parse_args()

    in_path = Path(args.input_html).expanduser().resolve()
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
        body, extras, warnings, used = extract(in_path)
    except Exception as e:
        emit_failure(f"extraction failed: {e}", extra={"input": str(in_path)})
        return 1

    fm = {
        "id": args.doc_id,
        "source": source_rel,
        "source_type": "html",
        "source_sha256": sha256_of(in_path),
        "extraction_method": f"{used}@{tool_version_string()}",
        "extraction_date": today_iso(),
        "source_encoding": extras.get("source_encoding", "utf-8"),
        "headings": extras.get("headings", []),
        "tokens_estimated": count_tokens(body),
        "warnings": warnings,
    }
    write_md(out_path, fm, body)
    emit_success(out_path, body, warnings, extra={"used": used})
    return 0


if __name__ == "__main__":
    sys.exit(main())
