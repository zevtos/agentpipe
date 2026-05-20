#!/usr/bin/env python3
"""
render_graph.py — Mermaid citation-graph renderer (Stage 3).

Emits a `graph LR` Mermaid block from the `citations` table, capping at
50 nodes (Mermaid hard-fails above ~80 — we leave headroom). Used by
synthesize.py to embed a "Citation Graph" section in the final report.

CLI:
    render_graph.py --paper-ids '["abc","def",...]' [--max-nodes 50]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from _common import emit_failure, json_stdout, log
from index import open_corpus


DEFAULT_MAX_NODES = 50
MAX_NODES_HARD = 80      # Mermaid renderer fails above this


def _short_label(title: str | None, doi: str | None, max_len: int = 24) -> str:
    if title:
        t = title.strip()
        if len(t) > max_len:
            t = t[: max_len - 1].rstrip() + "…"
        return t.replace('"', "'")
    if doi:
        return doi
    return "(untitled)"


def _node_id(idx: int) -> str:
    return f"P{idx}"


def render(con: sqlite3.Connection, paper_ids: list[str], *,
           max_nodes: int = DEFAULT_MAX_NODES) -> str:
    """Return a Mermaid `graph LR` block describing backward/forward citation
    edges among the given paper_ids."""
    if not paper_ids:
        return ""
    paper_ids = paper_ids[: min(max_nodes, MAX_NODES_HARD)]
    placeholders = ",".join("?" * len(paper_ids))

    # Fetch node metadata
    rows = con.execute(
        f"SELECT paper_id, title, doi, year, retracted_at "
        f"FROM papers WHERE paper_id IN ({placeholders})",
        paper_ids,
    ).fetchall()
    node_map: dict[str, dict] = {r["paper_id"]: dict(r) for r in rows}
    if not node_map:
        return ""

    # Stable index for Mermaid node ids (preserve input order)
    indexed = [pid for pid in paper_ids if pid in node_map]
    idx_of = {pid: i for i, pid in enumerate(indexed)}

    # Fetch edges between these papers (use cited_paper_id, not cited_doi —
    # only edges where both endpoints are in our subset)
    edges = con.execute(
        f"SELECT paper_id, cited_paper_id, direction, source_api "
        f"FROM citations "
        f"WHERE paper_id IN ({placeholders}) "
        f"  AND cited_paper_id IN ({placeholders})",
        paper_ids + paper_ids,
    ).fetchall()

    lines: list[str] = ["graph LR"]
    # Node declarations
    for pid in indexed:
        meta = node_map[pid]
        nid = _node_id(idx_of[pid])
        label = _short_label(meta.get("title"), meta.get("doi"))
        year = meta.get("year")
        if year:
            label += f" ({year})"
        retracted = meta.get("retracted_at")
        # Brackets escape: Mermaid uses {} () [] for shapes; sanitise the label
        safe = (label.replace("[", "(").replace("]", ")")
                     .replace('"', "'"))
        if retracted:
            lines.append(f'    {nid}["⚠ {safe}"]:::retracted')
        else:
            lines.append(f'    {nid}["{safe}"]')

    # Edges
    n_edges = 0
    for e in edges:
        src_pid = e["paper_id"]
        dst_pid = e["cited_paper_id"]
        if src_pid not in idx_of or dst_pid not in idx_of:
            continue
        src = _node_id(idx_of[src_pid])
        dst = _node_id(idx_of[dst_pid])
        # backward: src cites dst → arrow from src to dst
        # forward:  dst cites src → arrow from dst to src (we still log src→dst
        # but flip the visual direction by swapping)
        if e["direction"] == "forward":
            src, dst = dst, src
        lines.append(f"    {src} --> {dst}")
        n_edges += 1

    if any(r.get("retracted_at") for r in node_map.values()):
        lines.append("    classDef retracted fill:#fee,stroke:#c00,stroke-width:2px")

    if n_edges == 0:
        lines.append("    %% no internal citation edges found among these papers")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(prog="render_graph.py")
    parser.add_argument("--paper-ids", required=True,
                        help='JSON list: \'["abc","def"]\'')
    parser.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES)
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    try:
        ids = json.loads(args.paper_ids)
    except json.JSONDecodeError as e:
        emit_failure(f"bad --paper-ids: {e}")
        return 2

    try:
        con = open_corpus(Path(args.db) if args.db else None)
    except RuntimeError as e:
        emit_failure(str(e))
        return 1
    try:
        graph = render(con, ids, max_nodes=args.max_nodes)
    finally:
        con.close()

    if not graph:
        json_stdout({"ok": False, "reason": "no_nodes"})
        return 0
    print("```mermaid")
    print(graph)
    print("```")
    return 0


if __name__ == "__main__":
    sys.exit(main())
