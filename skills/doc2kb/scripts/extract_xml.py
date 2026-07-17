#!/usr/bin/env python3
"""
extract_xml.py — Phase 4 extractor for XML files.

CLI:
    extract_xml.py <input_xml> <output_md>
                   [--doc-id doc-NNN]
                   [--source-rel rel/path.xml]

Pipeline:
    1. Parse raw bytes with lxml.etree (recover=True) — honours the XML
       prolog encoding declaration, survives mildly malformed input.
    2. XHTML root (localname `html`) → reuse the HTML route from
       extract_html.py (trafilatura → markdownify fallback).
    3. Generic XML → flatten to text blocks: element text/tail nodes in
       document order, one paragraph per node. Tags and attributes are
       dropped (warning recorded). If the text yield is tiny but the file
       is not (attribute-heavy data XML), attribute values are appended
       as a salvage pass with their own warning.
    4. Parse failure → raw text passthrough (encoding-detected), warning
       recorded. Never fails on malformed XML.
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
from extract_html import _fallback_markdownify, _try_trafilatura  # noqa: E402


MIN_BODY_CHARS = 50
# Attribute salvage only makes sense when the file plausibly holds content
# that the text-node walk missed — tiny files with tiny yield are just tiny.
SALVAGE_MIN_FILE_BYTES = 1024


def _localname(tag: object) -> str:
    """Namespace-stripped lowercase tag name; '' for comments/PIs (whose
    .tag is a callable, not a string)."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _parse_xml(data: bytes):
    from lxml import etree  # type: ignore

    parser = etree.XMLParser(recover=True, resolve_entities=False,
                             no_network=True, huge_tree=False)
    root = etree.fromstring(data, parser=parser)
    if root is None:
        raise ValueError("lxml recovered nothing — not XML?")
    return root


def _walk_text_blocks(root) -> list[str]:
    blocks: list[str] = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue  # comment / processing instruction
        text = (el.text or "").strip()
        if text:
            blocks.append(text)
        tail = (el.tail or "").strip()
        if tail:
            blocks.append(tail)
    return blocks


def _walk_attr_values(root) -> list[str]:
    values: list[str] = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for v in el.attrib.values():
            v = v.strip()
            if v:
                values.append(v)
    return values


def _collect_headings(body: str) -> list[str]:
    headings = []
    for line in body.split("\n"):
        if line.startswith("# ") and len(line) < 200:
            sanitized = sanitize_heading(line[2:])
            if sanitized:
                headings.append(sanitized)
            if len(headings) >= 10:
                break
    return headings


def extract(input_path: Path) -> tuple[str, dict, list[str], str]:
    warnings: list[str] = []
    extras: dict = {}
    data = input_path.read_bytes()

    try:
        root = _parse_xml(data)
    except Exception as e:
        # Raw-text passthrough: malformed XML still lands in the KB.
        enc = detect_text_encoding(input_path)
        warnings.append(f"XML parse failed ({e}); raw text passthrough, tags kept verbatim")
        body = clean_whitespace(input_path.read_text(encoding=enc, errors="replace"))
        extras["source_encoding"] = enc
        return body, extras, warnings, "raw-text"

    extras["source_encoding"] = "utf-8"

    if _localname(root.tag) == "html":
        # XHTML — the HTML route understands it natively.
        html_text = data.decode("utf-8", errors="replace")
        used = "trafilatura"
        body = _try_trafilatura(html_text)
        if not body or len(body.strip()) < MIN_BODY_CHARS:
            warnings.append("trafilatura returned little/no content; falling back to markdownify")
            body = _fallback_markdownify(html_text)
            used = "markdownify"
        if not body:
            body = ""
            warnings.append("could not extract any content from XHTML")
        body = clean_whitespace(body)
        extras["headings"] = _collect_headings(body)
        return body, extras, warnings, used

    # Generic XML: text nodes in document order, one paragraph per node.
    blocks = _walk_text_blocks(root)
    body = clean_whitespace("\n\n".join(blocks))
    warnings.append("generic XML: tags/attributes stripped, structure flattened to text blocks")

    if len(body) < MIN_BODY_CHARS and len(data) > SALVAGE_MIN_FILE_BYTES:
        # Attribute-heavy data XML (<item name="…"/>) — text walk yields
        # nothing; attribute values are the content.
        attr_values = _walk_attr_values(root)
        if attr_values:
            warnings.append(f"text yield tiny; salvaged {len(attr_values)} attribute values")
            body = clean_whitespace(body + "\n\n" + "\n".join(attr_values))

    if not body:
        warnings.append("could not extract any text content from XML")

    extras["headings"] = []
    return body, extras, warnings, "lxml-xml"


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract XML → Markdown/text via lxml.")
    ap.add_argument("input_xml")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    args = ap.parse_args()

    in_path = Path(args.input_xml).expanduser().resolve()
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
        "source_type": "xml",
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
