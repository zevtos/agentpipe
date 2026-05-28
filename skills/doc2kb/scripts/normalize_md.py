#!/usr/bin/env python3
"""
normalize_md.py — structural cleanup pass over an extracted Markdown file.

CLI:
    normalize_md.py <input_md> [--write] [--page-recurrence 0.7]

Effect:
    - Default mode is DRY-RUN: prints a JSON report listing what would be
      removed (header/footer candidates, boilerplate patterns matched).
    - With --write: overwrites <input_md> in place and re-counts tokens in
      its frontmatter (`tokens_estimated` field).

Operations (NEVER summarization or rewriting):
    1. Detect lines that repeat on >page_recurrence fraction of [page N]
       blocks and remove them (header/footer dedup).
    2. Strip well-known boilerplate regexes (Page X of Y, copyright lines,
       "Click here…" tails, "JavaScript required" notices).
    3. Collapse 3+ blank lines to 2.
    4. Strip ASCII control characters (except \\n and \\t).
    5. Trim trailing whitespace on each line.

What this does NOT do:
    - No summarization, no paraphrasing, no translation.
    - No removal of section headings even if they look redundant.
    - No reordering of content.

The function is idempotent: running it twice changes nothing.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from _common import (  # noqa: E402
    PAGE_ANCHOR_RE as _PAGE_ANCHOR_RE,
    clean_whitespace,
    count_tokens,
    json_stdout,
    log,
    read_body,
    read_frontmatter,
    recover_ligatures,
    render_frontmatter,
    strip_page_footer_numbers,
)
_BOILERPLATE_PATTERNS = [
    re.compile(r"^\s*Page\s+\d+(\s+of\s+\d+)?\s*$", re.IGNORECASE),
    re.compile(r"^\s*Стр\.?\s+\d+(\s+из\s+\d+)?\s*$", re.IGNORECASE),
    re.compile(r"^\s*©.{0,80}$"),
    re.compile(r"^\s*Copyright\s+©.{0,80}$", re.IGNORECASE),
    re.compile(r"^\s*Click here.{0,60}$", re.IGNORECASE),
    re.compile(r"^\s*JavaScript\s+(is\s+)?required.*$", re.IGNORECASE),
    re.compile(r"^\s*Please\s+enable\s+JavaScript.*$", re.IGNORECASE),
    re.compile(r"^\s*Confidential\s*$", re.IGNORECASE),
    re.compile(r"^\s*Конфиденциально\s*$", re.IGNORECASE),
]
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _split_by_pages(body: str) -> list[tuple[str, list[str]]]:
    """Returns list of (anchor_line_or_None, page_lines).

    The first chunk before any [page N] anchor (if any) is returned with
    anchor=None. Anchor lines themselves are excluded from page_lines but
    preserved as separators in the returned tuples."""
    matches = list(_PAGE_ANCHOR_RE.finditer(body))
    if not matches:
        return [(None, body.split("\n"))]
    chunks: list[tuple[str | None, list[str]]] = []
    # Content before first page anchor (preamble — rare for pdf, but
    # we keep it intact).
    if matches[0].start() > 0:
        pre = body[: matches[0].start()].rstrip()
        if pre:
            chunks.append((None, pre.split("\n")))
    for i, m in enumerate(matches):
        anchor_line = m.group(0).rstrip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        page_body = body[body_start:body_end].strip("\n")
        chunks.append((anchor_line, page_body.split("\n") if page_body else []))
    return chunks


def _join_pages(chunks: list[tuple[str | None, list[str]]]) -> str:
    pieces: list[str] = []
    for anchor, lines in chunks:
        block = "\n".join(lines).strip("\n")
        if anchor:
            pieces.append(anchor + "\n\n" + block if block else anchor)
        elif block:
            pieces.append(block)
    return ("\n\n".join(pieces).strip() + "\n") if pieces else ""


def detect_recurring_lines(chunks: list[tuple[str | None, list[str]]],
                           threshold: float = 0.7,
                           min_pages: int = 4) -> set[str]:
    """Find lines that recur on at least threshold fraction of pages.
    Only considers the first 3 and last 3 non-empty lines of each page —
    that is where headers/footers live. Lines must be ≤80 chars (avoid
    accidentally removing real content sentences)."""
    page_chunks = [lines for anchor, lines in chunks if anchor is not None]
    if len(page_chunks) < min_pages:
        return set()
    candidate_counts: dict[str, int] = {}
    for lines in page_chunks:
        # First & last 3 non-empty lines
        non_empty = [ln.strip() for ln in lines if ln.strip()]
        sample = set(non_empty[:3] + non_empty[-3:])
        for line in sample:
            if 0 < len(line) <= 80:
                candidate_counts[line] = candidate_counts.get(line, 0) + 1
    n_pages = len(page_chunks)
    min_count = max(int(n_pages * threshold), 3)
    return {line for line, cnt in candidate_counts.items() if cnt >= min_count}


def filter_lines(lines: list[str], drop_set: set[str]) -> tuple[list[str], dict[str, int]]:
    """Drops lines whose stripped form is in drop_set, and lines matching
    any boilerplate pattern. Returns (kept_lines, removed_counts)."""
    removed: dict[str, int] = {}
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped in drop_set:
            removed["recurring"] = removed.get("recurring", 0) + 1
            continue
        matched = False
        for pat in _BOILERPLATE_PATTERNS:
            if pat.match(line):
                removed[pat.pattern] = removed.get(pat.pattern, 0) + 1
                matched = True
                break
        if matched:
            continue
        kept.append(line)
    return kept, removed


def normalize(body: str, threshold: float = 0.7) -> tuple[str, dict]:
    # Drop standalone footer page numbers between [page N] anchors first,
    # so detect_recurring_lines doesn't see them as candidate-content.
    body, n_footer = strip_page_footer_numbers(body)
    # Repair any residual ligature drops left by pymupdf4llm. Safe to run
    # on already-clean text — recover_ligatures is idempotent.
    body, n_lig = recover_ligatures(body)

    chunks = _split_by_pages(body)
    recurring = detect_recurring_lines(chunks, threshold=threshold)

    new_chunks: list[tuple[str | None, list[str]]] = []
    total_removed: dict[str, int] = {}
    for anchor, lines in chunks:
        filtered, removed = filter_lines(lines, recurring)
        for k, v in removed.items():
            total_removed[k] = total_removed.get(k, 0) + v
        new_chunks.append((anchor, filtered))

    new_body = _join_pages(new_chunks)
    # Strip control characters that snuck through.
    new_body, n_ctrl = _CONTROL_RE.subn("", new_body)
    if n_ctrl:
        total_removed["control_chars"] = n_ctrl
    if n_footer:
        total_removed["footer_page_numbers"] = n_footer
    if n_lig:
        total_removed["ligatures_recovered"] = n_lig
    new_body = clean_whitespace(new_body)

    report = {
        "recurring_lines": sorted(recurring),
        "removed_counts": total_removed,
        "before_chars": len(body),
        "after_chars": len(new_body),
        "chars_saved": len(body) - len(new_body),
    }
    return new_body, report


def main() -> int:
    ap = argparse.ArgumentParser(description="Structural cleanup of an extracted .md file.")
    ap.add_argument("input_md")
    ap.add_argument("--write", action="store_true",
                    help="Overwrite the file in place (default: dry-run, JSON report only).")
    ap.add_argument("--page-recurrence", type=float, default=0.7,
                    help="Fraction of pages a line must appear on to count as header/footer.")
    args = ap.parse_args()

    p = Path(args.input_md).expanduser().resolve()
    if not p.is_file():
        json_stdout({"ok": False, "reason": f"file not found: {p}"})
        return 1

    fm = read_frontmatter(p)
    body = read_body(p)
    if not body.strip():
        if args.write:
            # Keep frontmatter consistent (tokens_estimated=0) even when
            # body is empty so build_manifest sees fresh data.
            fm["tokens_estimated"] = 0
            p.write_text(render_frontmatter(fm), encoding="utf-8")
        json_stdout({"ok": True, "input": str(p), "noop": True,
                     "reason": "empty body", "written": args.write,
                     "tokens_after": 0 if args.write else None})
        return 0

    new_body, report = normalize(body, threshold=args.page_recurrence)
    if args.write:
        # Update token estimate and append warnings if anything was cleaned.
        fm["tokens_estimated"] = count_tokens(new_body)
        # We don't touch fm["warnings"] — normalization isn't a problem.
        text = render_frontmatter(fm) + new_body
        p.write_text(text, encoding="utf-8")
        log(f"normalized {p.name}: {report['chars_saved']} chars removed")

    json_stdout({
        "ok": True,
        "input": str(p),
        "written": args.write,
        "tokens_after": count_tokens(new_body) if args.write else None,
        **report,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
