#!/usr/bin/env python3
"""
extract_subtitles.py — Phase 4 extractor for standalone subtitle / lyrics files
(.srt, .vtt/.webvtt, .ass/.ssa, .sbv, .lrc).

CLI:
    extract_subtitles.py <input_subs> <output_md>
                         [--doc-id doc-NNN] [--source-rel rel/path/in/corpus.srt]

Effect:
    Writes <output_md> (YAML frontmatter + Markdown) and prints one JSON line.
    Cue numbers, timing lines, styling tags (`<i>`, `{\\an8}`, ASS overrides)
    and the rolling duplicates of auto-generated captions are dropped; the
    text itself is verbatim. Body layout is the one extract_media.py uses —
    `[mm:ss]` paragraphs in `## mm:ss–mm:ss` sections — so a subtitle file and
    a transcript cite time the same way.

Pure stdlib + charset-normalizer (lightweight tier). Legacy encodings
(cp1251, koi8-r — common for Russian .srt) are detected, not assumed.
Subtitles that scout paired with a media file of the same name are extracted
INTO that media doc instead (extract_media --sidecar), not by this script.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import (  # noqa: E402
    count_tokens,
    detect_text_encoding,
    emit_failure,
    emit_success,
    sha256_of,
    today_iso,
    tool_version_string,
    validate_source_rel,
    write_md,
)
from _media import parse_subtitles, render_timed_body, sidecar_lang, subtitle_format  # noqa: E402


def extract(in_path: Path, name: str) -> tuple[str, dict, list[str]]:
    enc = detect_text_encoding(in_path, candidates=("cp1251", "koi8-r", "cp1252", "iso-8859-1"),
                               validator=lambda t: "�" not in t)
    text = in_path.read_bytes().decode(enc, errors="replace")
    fmt = subtitle_format(in_path, text)
    cues = parse_subtitles(text, fmt)
    warnings: list[str] = []
    if not cues:
        warnings.append(f"media: no subtitle cues found ({fmt} parser) — file may be empty "
                        "or not a subtitle file")
    note = "Text of the subtitles; [mm:ss] = start of the cue."
    body, _ = render_timed_body(name, cues, duration=None, note=note)
    extras: dict = {"subtitle_format": fmt, "cues": len(cues), "source_encoding": enc}
    if cues:
        extras["duration_seconds"] = int(round(max(c.end for c in cues)))
    lang = sidecar_lang(in_path, in_path.stem.rsplit(".", 1)[0]) if "." in in_path.stem else None
    if lang:
        extras["language"] = lang
    return body, extras, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract a subtitle file → timed Markdown.")
    ap.add_argument("input_subs")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    args = ap.parse_args()

    in_path = Path(args.input_subs).expanduser().resolve()
    out_path = Path(args.output_md).expanduser().resolve()
    try:
        source_rel = validate_source_rel(args.source_rel or in_path.name)
    except ValueError as e:
        emit_failure(f"invalid --source-rel: {e}")
        return 1
    if not in_path.is_file():
        emit_failure(f"input not found: {in_path}")
        return 1
    try:
        body, extras, warnings = extract(in_path, Path(source_rel).name)
    except Exception as e:  # noqa: BLE001
        emit_failure(f"extraction failed: {e}", extra={"input": str(in_path)})
        return 1
    fm = {
        "id": args.doc_id,
        "source": source_rel,
        "source_type": "subtitles",
        "source_sha256": sha256_of(in_path),
        "extraction_method": f"subtitles-{extras['subtitle_format']}@{tool_version_string()}",
        "extraction_date": today_iso(),
        "headings": [],
        **extras,
        "tokens_estimated": count_tokens(body),
        "warnings": warnings,
    }
    write_md(out_path, fm, body)
    emit_success(out_path, body, warnings, extra={"extractor": f"subtitles-{extras['subtitle_format']}",
                                                  "cues": extras["cues"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
