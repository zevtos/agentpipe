#!/usr/bin/env python3
"""
extract_md_txt.py — Phase 4 extractor for Markdown and plain-text files.

CLI:
    extract_md_txt.py <input_file> <output_md>
                      [--doc-id doc-NNN]
                      [--source-rel rel/path.md]

Effect:
    - Detects encoding via charset-normalizer (fallback to utf-8).
    - Strips BOM, normalizes CRLF→LF, collapses trailing whitespace.
    - For .md: if the source already has YAML frontmatter, parses it and
      merges its `title`/`tags` (if present) into our frontmatter, then
      strips the source frontmatter from the body (we own the manifest now).
    - For .txt: body is kept as-is after whitespace cleanup.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from _common import (  # noqa: E402
    FRONTMATTER_RE as _FRONTMATTER_RE,
    clean_whitespace,
    count_tokens,
    detect_text_encoding,
    emit_failure,
    emit_success,
    parse_frontmatter_text,
    sanitize_heading,
    sha256_of,
    today_iso,
    tool_version_string,
    validate_source_rel,
    write_md,
)


EXTRACTOR_NAME_MD = "passthrough-md"
EXTRACTOR_NAME_TXT = "passthrough-txt"
_BOM = "﻿"


def _looks_like_real_text(s: str, min_ratio: float = 0.75) -> bool:
    """Returns True if at least min_ratio of chars are 'sensible' — basic
    latin, cyrillic, common punctuation, whitespace. Used to validate
    candidate decodings that don't raise (cp1251 happily decodes any byte
    sequence; we still need to know whether the result is readable)."""
    if not s:
        return False
    good = 0
    for c in s:
        o = ord(c)
        if o < 32 and c not in "\n\r\t":
            continue
        if (
            32 <= o < 127         # printable ASCII
            or 0x0400 <= o < 0x0500  # cyrillic block
            or o in (0x00A0, 0x00AB, 0x00BB, 0x2013, 0x2014, 0x2019, 0x201C, 0x201D)
        ):
            good += 1
    return good / len(s) >= min_ratio


def _detect_encoding(path: Path) -> str:
    """BOM → UTF-8 → cp1251/cp1252 (validated) → charset-normalizer.

    UTF-8 is tried first because charset-normalizer often misidentifies
    short cyrillic samples as exotic codecs (big5, gb18030) while the same
    bytes decode cleanly as UTF-8. CP1251 explicitly next — for Russian
    corpora it's the most common non-UTF-8 codec, and charset-normalizer
    struggles on short cp1251 samples."""
    return detect_text_encoding(
        path,
        candidates=("cp1251", "cp1252", "koi8-r", "iso-8859-1"),
        validator=_looks_like_real_text,
    )


def _strip_source_frontmatter(text: str) -> tuple[str, dict]:
    """Split off a leading YAML frontmatter block (if any). Uses the same
    parser as read_frontmatter for consistency — supports block lists and
    CRLF line endings — so `tags:\\n  - a\\n  - b` in a source .md round-trips
    correctly into `original_tags`."""
    # Normalize line endings before regex match — Windows-authored sources
    # would otherwise leak their frontmatter into the kb body.
    if "\r\n" in text[:200]:
        text = text.replace("\r\n", "\n")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return text, {}
    src_fm = parse_frontmatter_text(text[: m.end()])
    return text[m.end():], src_fm


def extract_md(input_path: Path, encoding: str) -> tuple[str, dict, list[str]]:
    warnings: list[str] = []
    try:
        raw = input_path.read_text(encoding=encoding, errors="replace")
    except Exception as e:
        raise RuntimeError(f"read failed: {e}") from e
    if raw.startswith(_BOM):
        raw = raw[1:]
    body, src_fm = _strip_source_frontmatter(raw)
    body = clean_whitespace(body)

    extras: dict = {}
    title = src_fm.get("title")
    if title:
        extras["original_title"] = title
    tags = src_fm.get("tags")
    if tags:
        extras["original_tags"] = tags

    headings = []
    for line in body.split("\n"):
        if line.startswith("# ") and len(line) < 200:
            sanitized = sanitize_heading(line[2:])
            if sanitized:
                headings.append(sanitized)
            if len(headings) >= 10:
                break
    extras["headings"] = headings
    return body, extras, warnings


def extract_txt(input_path: Path, encoding: str) -> tuple[str, dict, list[str]]:
    warnings: list[str] = []
    try:
        raw = input_path.read_text(encoding=encoding, errors="replace")
    except Exception as e:
        raise RuntimeError(f"read failed: {e}") from e
    if raw.startswith(_BOM):
        raw = raw[1:]
    body = clean_whitespace(raw)
    return body, {}, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract Markdown/Text → normalized Markdown.")
    ap.add_argument("input_file")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    ap.add_argument("--mode", choices=["md", "txt", "auto"], default="auto",
                    help="Force md/txt handling, or autodetect by extension.")
    args = ap.parse_args()

    in_path = Path(args.input_file).expanduser().resolve()
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

    mode = args.mode
    if mode == "auto":
        suf = in_path.suffix.lower()
        mode = "md" if suf in (".md", ".markdown") else "txt"

    enc = _detect_encoding(in_path)

    try:
        if mode == "md":
            body, extras, warnings = extract_md(in_path, enc)
            method = EXTRACTOR_NAME_MD
            stype = "md"
        else:
            body, extras, warnings = extract_txt(in_path, enc)
            method = EXTRACTOR_NAME_TXT
            stype = "txt"
    except Exception as e:
        emit_failure(f"extraction failed: {e}", extra={"input": str(in_path)})
        return 1

    if enc and enc.lower() not in ("utf-8", "ascii"):
        warnings.append(f"source encoding {enc} converted to UTF-8")

    fm = {
        "id": args.doc_id,
        "source": source_rel,
        "source_type": stype,
        "source_sha256": sha256_of(in_path),
        "extraction_method": f"{method}@{tool_version_string()}",
        "extraction_date": today_iso(),
        "source_encoding": enc,
        "headings": extras.get("headings", []),
        "tokens_estimated": count_tokens(body),
        "warnings": warnings,
    }
    if "original_title" in extras:
        fm["original_title"] = extras["original_title"]
    if "original_tags" in extras:
        fm["original_tags"] = extras["original_tags"]
    write_md(out_path, fm, body)
    emit_success(out_path, body, warnings, extra={"source_encoding": enc})
    return 0


if __name__ == "__main__":
    sys.exit(main())
