#!/usr/bin/env python3
"""
shard_doc.py — Phase 4 shard sub-pass (opt-in): split oversized extracted docs into folders.

CLI:
    shard_doc.py <kb_dir> [--threshold 24000] [--target 8000]
                          [--min 2000] [--cap 12000] [--quiet]

Effect:
    For every flat `<kb_dir>/docs/<id>-<slug>.md` whose `tokens_estimated`
    exceeds the trigger threshold, replaces it with a FOLDER:

        docs/<id>-<slug>/
            _index.md         # parent: frontmatter + generated TOC (no body)
            001-<slug>.md      # verbatim part (one structural unit or a pack of them)
            002-<slug>.md
            ...

    Prints ONE JSON summary line. Small docs are left flat, untouched.

Why (the problem this solves):
    Search is already passage-level (index_kb chunks every doc into ~400-token
    BM25 passages with citations). Sharding does NOT change recall. Its sole job
    is that the citation-target FILE an agent opens after a hit is small enough
    to Read whole — a 300k-token book becomes a folder of ~8k-token chapter
    files, and a `query.sh` hit cites the one chapter, not the book.

Split strategy (heading-first cascade — never a crude global token cut):
    1. Split on the top Markdown heading level present (`#`, then deeper levels
       only inside an oversized unit). For EPUB the pandoc output emits one H1
       per spine chapter, so this lands exactly on chapter boundaries.
    2. PDFs with no headings fall back to `[page N]` anchors as the grid.
    3. A doc with no headings and no page anchors falls back to paragraph-pack.
    4. Token target is used ONLY to pack small units together and to sub-divide
       a single unit that exceeds the cap — and even then the cut is on a
       blank-line paragraph boundary, never mid-paragraph, never mid-sentence,
       never inside a fenced code block.

Balance: greedy pack toward `target`, merge any unit below `min` forward, so no
    part is a 100-token orphan next to a 50k-token giant. Every part lands in
    roughly [min, cap]; the only way past `cap` is a single indivisible
    paragraph (flagged `part_oversize`).

Verbatim guarantee (Rule #1 — never summarize): the pass reassembles the parts
    in memory and asserts they reproduce the original body (modulo whitespace
    normalization and the asset-link depth rewrite). On ANY mismatch it ABORTS
    that doc — leaves it flat, logs `shard_failed: reassembly_mismatch` — so a
    sharder bug can never silently corrupt content.

Idempotent: a doc already sharded into a folder with a matching `source_sha256`
    is a no-op; a source change (new sha) re-shards from the fresh flat file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from _common import (  # noqa: E402
    HEADING_RE,
    clean_whitespace,
    count_tokens,
    log as _common_log,
    read_body,
    read_frontmatter,
    sanitize_heading,
    slugify,
    split_markdown_blocks,
    today_iso,
    tool_version_string,
    write_md,
)

SHARD_SCHEMA_VERSION = "1"

# Token thresholds (tiktoken cl100k_base, same unit as `tokens_estimated`).
DEFAULT_TRIGGER = 24_000   # below this a flat file Reads fine — never shard
DEFAULT_TARGET = 8_000     # pack parts toward this size
DEFAULT_MIN = 2_000        # no part below this (except the last) — kills orphans
DEFAULT_CAP = 12_000       # hard ceiling; only an indivisible paragraph exceeds it

INDEX_NAME = "_index.md"

# Asset links sit one directory shallower than a part file: a flat doc at
# docs/x.md links `../assets/…`; a part at docs/x/001.md needs `../../assets/…`.
_ASSET_LINK_RE = re.compile(r"\]\(\.\./assets/")
_ASSET_LINK_DEEP = "](../../assets/"
# A block that is exactly a `[page N]` anchor (used for fence-aware page splits —
# matched against whole blocks from split_markdown_blocks, so a `[page N]` line
# INSIDE a fenced code block is never mistaken for a page boundary).
_PAGE_ANCHOR_BLOCK_RE = re.compile(r"\[page (\d+)\]")


def log(msg: str) -> None:
    _common_log(msg, prefix="doc2kb shard")


def _blocks_to_text(blocks: list[str]) -> str:
    return "\n\n".join(blocks)


def _heading_level(block: str) -> Optional[int]:
    m = HEADING_RE.match(block)
    return len(m.group(1)) if m else None


def _heading_text(block: str) -> str:
    m = HEADING_RE.match(block)
    return sanitize_heading(m.group(2), maxlen=120) if m else ""


def _split_blocks_by_level(blocks: list[str], level: int) -> list[tuple[str, list[str]]]:
    """Group blocks into (title, blocks) units, starting a new unit at each
    heading of exactly `level`. Content before the first such heading is a
    leading unit with an empty title."""
    units: list[tuple[str, list[str]]] = []
    cur_title = ""
    cur: list[str] = []
    for b in blocks:
        if _heading_level(b) == level:
            if cur:
                units.append((cur_title, cur))
            cur_title = _heading_text(b)
            cur = [b]
        else:
            cur.append(b)
    if cur:
        units.append((cur_title, cur))
    return units


def _split_blocks_by_page(blocks: list[str]) -> list[tuple[str, list[str]]]:
    """Group blocks into (title, blocks) units starting a new unit at each block
    that is exactly a `[page N]` anchor. Because `blocks` comes from
    split_markdown_blocks (which keeps fenced code intact), a `[page N]` line
    inside a code fence is part of that fenced block and never starts a unit —
    so the split is fence-aware, unlike a raw MULTILINE regex over the body."""
    units: list[tuple[str, list[str]]] = []
    cur_title = ""
    cur: list[str] = []
    for b in blocks:
        m = _PAGE_ANCHOR_BLOCK_RE.fullmatch(b.strip())
        if m:
            if cur:
                units.append((cur_title, cur))
            cur_title = f"page {m.group(1)}"
            cur = [b]
        else:
            cur.append(b)
    if cur:
        units.append((cur_title, cur))
    return units


def _paragraph_split(text: str, target: int) -> list[str]:
    """Pack blocks toward `target`, closing a piece before it would exceed it.
    A single block larger than `target` is left whole (verbatim wins over
    balance). Splits only on block boundaries — never inside a paragraph or
    code fence."""
    blocks = split_markdown_blocks(text)
    out: list[str] = []
    cur: list[str] = []
    tok = 0
    for b in blocks:
        bt = count_tokens(b)
        if cur and tok + bt > target:
            out.append(_blocks_to_text(cur))
            cur, tok = [], 0
        cur.append(b)
        tok += bt
    if cur:
        out.append(_blocks_to_text(cur))
    return out or [text]


def _expand_unit(title: str, text: str, target: int, cap: int) -> list[tuple[str, str]]:
    """Ensure a unit is ≤ cap by splitting deeper: first at the shallowest
    sub-heading level that actually divides it, else on paragraph boundaries."""
    if count_tokens(text) <= cap:
        return [(title, text)]
    blocks = split_markdown_blocks(text)
    sub_levels = sorted({lv for b in blocks if (lv := _heading_level(b)) is not None})
    for lvl in sub_levels:
        subs = _split_blocks_by_level(blocks, lvl)
        if len(subs) > 1:
            out: list[tuple[str, str]] = []
            for st, sblocks in subs:
                out.extend(_expand_unit(st or title, _blocks_to_text(sblocks), target, cap))
            return out
    # No usable sub-heading — paragraph-pack the body.
    return [(title, piece) for piece in _paragraph_split(text, target)]


def build_units(body: str, source_type: str, target: int,
                cap: int) -> list[tuple[str, str]]:
    """Return ordered (title, text) structural units via the heading-first
    cascade. Concatenating the unit texts reproduces `body` (modulo the blank
    line joins that clean_whitespace normalizes)."""
    blocks = split_markdown_blocks(body)
    levels = sorted({lv for b in blocks if (lv := _heading_level(b)) is not None})

    raw_units: list[tuple[str, str]]
    if levels:
        top = levels[0]
        raw_units = [(t, _blocks_to_text(b)) for t, b in _split_blocks_by_level(blocks, top)]
    else:
        # No headings: try PDF `[page N]` anchors as the grid (fence-aware,
        # block-based), else fall through to one unit (paragraph-packed below).
        page_units = _split_blocks_by_page(blocks)
        if len(page_units) > 1:
            raw_units = [(t, _blocks_to_text(b)) for t, b in page_units]
        else:
            raw_units = [("", body)]

    units: list[tuple[str, str]] = []
    for title, text in raw_units:
        units.extend(_expand_unit(title, text, target, cap))
    return units


def pack_units(units: list[tuple[str, str]], target: int, minimum: int,
               cap: int) -> list[dict[str, Any]]:
    """Greedy pack adjacent units toward `target`, merging any run below
    `minimum` forward, so parts cluster in [minimum, cap]. Returns part dicts
    {title, text, tokens, oversize}."""
    parts: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    for title, text in units:
        ut = count_tokens(text)
        if cur is None:
            cur = {"title": title, "text": text, "tokens": ut}
            continue
        should_merge = (cur["tokens"] < minimum) or (
            cur["tokens"] < target and cur["tokens"] + ut <= cap)
        if should_merge:
            cur["text"] = cur["text"] + "\n\n" + text
            cur["tokens"] += ut
            if not cur["title"]:
                cur["title"] = title
        else:
            parts.append(cur)
            cur = {"title": title, "text": text, "tokens": ut}
    if cur is not None:
        if parts and cur["tokens"] < minimum:
            parts[-1]["text"] += "\n\n" + cur["text"]
            parts[-1]["tokens"] += cur["tokens"]
        else:
            parts.append(cur)
    for p in parts:
        p["oversize"] = p["tokens"] > cap
    return parts


def _part_headings(text: str, limit: int = 8) -> list[str]:
    out: list[str] = []
    for line in text.split("\n"):
        if line.startswith("#"):
            h = _heading_text(line)
            if h:
                out.append(h)
            if len(out) >= limit:
                break
    return out


def _sha256_str(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rewrite_asset_links(text: str) -> str:
    """Deepen `](../assets/` → `](../../assets/` for a part file one level below
    the flat doc. Touches link targets only; body text is untouched."""
    return _ASSET_LINK_RE.sub(_ASSET_LINK_DEEP, text)


def shard_one(doc_path: Path, *, trigger: int = DEFAULT_TRIGGER,
              target: int = DEFAULT_TARGET, minimum: int = DEFAULT_MIN,
              cap: int = DEFAULT_CAP) -> dict[str, Any]:
    """Shard a single flat doc into a folder if it exceeds `trigger` and has
    usable structure. Returns a result dict (status ∈ {sharded, flat, skipped,
    failed, unchanged})."""
    fm = read_frontmatter(doc_path)
    doc_id = fm.get("id")
    rel = f"docs/{doc_path.name}"
    if not doc_id:
        return {"status": "skipped", "doc": rel, "reason": "no id in frontmatter"}

    tokens = fm.get("tokens_estimated") or 0
    if tokens <= trigger:
        return {"status": "flat", "doc": rel, "tokens": tokens}

    body = read_body(doc_path)
    source_type = fm.get("source_type") or "unknown"

    units = build_units(body, source_type, target, cap)
    parts = pack_units(units, target, minimum, cap)

    if len(parts) < 2:
        # A single part means no benefit (and may still be over cap). Keep flat.
        return {"status": "flat", "doc": rel, "tokens": tokens,
                "reason": "no_structural_boundaries" if not units
                else "single_part_after_packing"}

    # --- Verbatim self-check BEFORE writing anything. `written` is exactly the
    # bytes each part file will contain (asset-link rewrite applied); comparing
    # their concat against the SAME rewrite of the original body makes the check
    # symmetric — a preexisting `../../assets/` link can't desync it. ---
    written = [_rewrite_asset_links(p["text"]) for p in parts]
    if clean_whitespace("\n\n".join(written)) != clean_whitespace(_rewrite_asset_links(body)):
        return {"status": "failed", "doc": rel,
                "reason": "reassembly_mismatch — kept flat"}

    folder = doc_path.with_suffix("")  # docs/<id>-<slug>/
    sha = fm.get("source_sha256") or ""

    # Idempotency: an existing folder for the same source is already current.
    existing_index = folder / INDEX_NAME
    if existing_index.is_file():
        ex_fm = read_frontmatter(existing_index)
        if (ex_fm.get("source_sha256") or "") == sha and sha:
            doc_path.unlink(missing_ok=True)  # drop the redundant flat re-extract
            return {"status": "unchanged", "doc": f"docs/{folder.name}/", "parts": len(parts)}
        shutil.rmtree(folder, ignore_errors=True)  # stale → re-shard

    folder.mkdir(parents=True, exist_ok=True)
    stem = folder.name  # <id>-<slug>
    base_slug = stem.split("-", 1)[1] if "-" in stem else stem
    width = max(3, len(str(len(parts))))

    part_files: list[str] = []
    oversize_warnings: list[str] = []
    for n, p in enumerate(parts, start=1):
        title = p["title"] or f"Part {n}"
        pslug = slugify(p["title"], maxlen=40) if p["title"] else "part"
        name = f"{n:0{width}d}-{pslug}.md"
        part_fm = {
            "id": doc_id,
            "source": fm.get("source"),
            "source_type": source_type,
            "source_sha256": sha,
            "parent": INDEX_NAME,
            "part_order": n,
            "part_title": title,
            "part_sha256": _sha256_str(p["text"]),
            "tokens_estimated": p["tokens"],
            "headings": _part_headings(p["text"]),
            "warnings": (["part_oversize: %d tokens exceed the cap "
                          "(indivisible content kept whole)" % p["tokens"]]
                         if p["oversize"] else []),
        }
        write_md(folder / name, part_fm, written[n - 1])
        part_files.append(name)
        if p["oversize"]:
            oversize_warnings.append(name)

    # Parent _index.md: frontmatter + generated TOC, no source body.
    toc_lines = [
        f"# {fm.get('source') or stem}",
        "",
        f"> Sharded document — {len(parts)} parts, ~{sum(p['tokens'] for p in parts):,} "
        "tokens. Concatenating the parts in order reproduces the source verbatim. "
        "After a search hit, open the cited part file, not this index.",
        "",
        "| # | Part | Tokens | File |",
        "|---|------|--------|------|",
    ]
    for n, (p, name) in enumerate(zip(parts, part_files), start=1):
        title = (p["title"] or f"Part {n}").replace("|", "\\|")
        toc_lines.append(f"| {n} | {title} | {p['tokens']:,} | [{name}]({name}) |")
    parent_fm = {
        "id": doc_id,
        "source": fm.get("source"),
        "source_type": source_type,
        "source_sha256": sha,
        "extraction_method": fm.get("extraction_method"),
        "tokens_estimated": sum(p["tokens"] for p in parts),
        "headings": fm.get("headings", []) or [],
        "warnings": fm.get("warnings", []) or [],
        "sharded": True,
        "shard_version": SHARD_SCHEMA_VERSION,
        "shard_tool": f"doc2kb-shard@{tool_version_string()}",
        "part_count": len(parts),
        "parts": part_files,
        "shard_date": today_iso(),
    }
    write_md(folder / INDEX_NAME, parent_fm, "\n".join(toc_lines) + "\n")

    # Replace the flat file only after the folder is fully written.
    doc_path.unlink(missing_ok=True)

    result = {"status": "sharded", "doc": f"docs/{folder.name}/",
              "parts": len(parts), "tokens": sum(p["tokens"] for p in parts)}
    if oversize_warnings:
        result["oversize_parts"] = oversize_warnings
    return result


def shard_corpus(kb_dir: Path, *, trigger: int = DEFAULT_TRIGGER,
                 target: int = DEFAULT_TARGET, minimum: int = DEFAULT_MIN,
                 cap: int = DEFAULT_CAP, quiet: bool = False) -> dict[str, Any]:
    """Shard every over-threshold flat doc under <kb_dir>/docs/. Non-fatal per
    doc: a failure leaves that doc flat and is reported, never raised."""
    kb_dir = Path(kb_dir).expanduser().resolve()
    docs_dir = kb_dir / "docs"
    counts = {"sharded": 0, "flat": 0, "skipped": 0, "failed": 0, "unchanged": 0}
    results: list[dict[str, Any]] = []
    if not docs_dir.is_dir():
        return {"ok": False, "kb_dir": str(kb_dir),
                "reason": f"no docs/ under {kb_dir}", "counts": counts}

    for doc_path in sorted(docs_dir.glob("*.md")):
        try:
            res = shard_one(doc_path, trigger=trigger, target=target,
                            minimum=minimum, cap=cap)
        except Exception as e:  # noqa: BLE001 — one bad doc never aborts the batch
            res = {"status": "failed", "doc": f"docs/{doc_path.name}",
                   "reason": f"{type(e).__name__}: {e}"}
        counts[res["status"]] = counts.get(res["status"], 0) + 1
        if res["status"] in ("sharded", "failed"):
            results.append(res)
        if not quiet and res["status"] in ("sharded", "failed", "unchanged"):
            log(f"{res['status']} {res['doc']}"
                + (f" ({res.get('parts')} parts)" if res.get("parts") else "")
                + (f": {res['reason']}" if res.get("reason") else ""))

    return {"ok": counts["failed"] == 0, "kb_dir": str(kb_dir),
            "counts": counts, "results": results,
            "thresholds": {"trigger": trigger, "target": target,
                           "min": minimum, "cap": cap}}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Phase 4 shard sub-pass (opt-in) — split oversized extracted docs into "
                    "per-chapter folders for Read-friendly citations.")
    ap.add_argument("kb_dir")
    ap.add_argument("--threshold", type=int, default=DEFAULT_TRIGGER,
                    help=f"shard only docs above this many tokens (default {DEFAULT_TRIGGER})")
    ap.add_argument("--target", type=int, default=DEFAULT_TARGET,
                    help=f"part token target (default {DEFAULT_TARGET})")
    ap.add_argument("--min", type=int, default=DEFAULT_MIN, dest="minimum",
                    help=f"part token floor — no part below this except the last (default {DEFAULT_MIN})")
    ap.add_argument("--cap", type=int, default=DEFAULT_CAP,
                    help=f"part token hard cap (default {DEFAULT_CAP})")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not (args.minimum < args.target <= args.cap <= args.threshold):
        print(json.dumps({"ok": False, "reason":
              "thresholds must satisfy min < target <= cap <= threshold"}))
        return 2

    summary = shard_corpus(Path(args.kb_dir), trigger=args.threshold,
                           target=args.target, minimum=args.minimum,
                           cap=args.cap, quiet=args.quiet)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main())
