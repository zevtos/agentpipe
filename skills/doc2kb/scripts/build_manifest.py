#!/usr/bin/env python3
"""
build_manifest.py — Phase 5: assemble manifest.json, INDEX.md, llms.txt,
AGENTS.md from extracted files under <kb_dir>/docs/.

CLI:
    build_manifest.py <kb_dir> [--scout <path>] [--quiet]

Effects:
    - Reads every <kb_dir>/docs/*.md frontmatter.
    - Optionally reads <kb_dir>/_scout.json for skipped/error info.
    - Writes <kb_dir>/manifest.json (UTF-8, indented JSON).
    - Writes <kb_dir>/INDEX.md (human + agent readable overview).
    - Writes <kb_dir>/llms.txt (llmstxt.org-compatible catalog).
    - Writes <kb_dir>/AGENTS.md (static, copied from assets/agents_template.md).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _common import (  # noqa: E402
    SCHEMA_VERSION_DOC,
    nfc_optional as _nfc,
    read_frontmatter,
    tool_version_string,
    utc_now_iso,
)


SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_AGENTS = SKILL_DIR / "assets" / "agents_template.md"


def collect_docs(kb_dir: Path) -> list[dict[str, Any]]:
    """Reads every kb/docs/*.md frontmatter and returns a list of dicts
    suitable for manifest.documents."""
    docs_dir = kb_dir / "docs"
    if not docs_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(docs_dir.glob("*.md")):
        fm = read_frontmatter(p)
        if not fm:
            continue
        rec: dict[str, Any] = {
            "id": fm.get("id"),
            "source_path": fm.get("source"),
            "kb_path": f"docs/{p.name}",
            "sha256": fm.get("source_sha256"),
            "source_type": fm.get("source_type"),
            "extraction_method": fm.get("extraction_method"),
            "tokens_estimated": fm.get("tokens_estimated", 0),
            "warnings": fm.get("warnings", []) or [],
        }
        # Per-type extras.
        for key in (
            "pages", "slides", "has_notes", "notes_chars",
            "inline_images", "has_tables", "has_equations",
            "has_charts", "has_tracked_changes", "paragraphs",
            "source_encoding", "headings",
            "cells", "code_cells", "markdown_cells", "raw_cells",
            "has_outputs", "language", "kernelspec_name",
        ):
            if key in fm:
                rec[key] = fm[key]
        out.append(rec)
    return out


def merge_scout(kb_dir: Path, manifest: dict[str, Any]) -> None:
    """Pull skipped/errors and per-file size_bytes from _scout.json if exists."""
    scout_path = kb_dir / "_scout.json"
    if not scout_path.is_file():
        return
    try:
        scout = json.loads(scout_path.read_text(encoding="utf-8"))
    except Exception:
        return
    manifest["corpus_root"] = scout.get("input_root")

    # Index scout files by NFC-normalized source_path so we can enrich
    # documents with original-file size_bytes (frontmatter doesn't carry
    # it). NFC matching tolerates older _scout.json files that stored
    # NFD-form paths from macOS pathlib.
    scout_by_source = {
        _nfc(f.get("source_path")): f for f in scout.get("files", []) or []
    }
    for d in manifest["documents"]:
        sf = scout_by_source.get(_nfc(d.get("source_path")))
        if sf and sf.get("size_bytes") is not None:
            d["size_bytes"] = sf["size_bytes"]

    # Files that scout flagged as skipped from the start.
    manifest["skipped"] = scout.get("skipped_at_scout", []) or []
    # Files scout knew about but we don't have a doc for — could be skipped
    # by user during decide phase, or extraction errors. Fold in. Use
    # NFC-normalized comparison so an NFD-form scout path doesn't
    # false-positive against an NFC-form frontmatter path.
    doc_sources = {_nfc(d.get("source_path")) for d in manifest["documents"]}
    for f in scout.get("files", []) or []:
        sp = f.get("source_path")
        if sp and _nfc(sp) not in doc_sources:
            # Most specific reason first.
            if f.get("action_required"):
                reason = f"deferred: {f['action_required']}"
            elif f.get("extraction_strategy") in ("skip", "not_in_mvp",
                                                  "needs_ocr_or_vlm",
                                                  "needs_password"):
                reason = f"strategy={f['extraction_strategy']}"
            else:
                # Doc was extractable but its kb file is missing — agent
                # likely failed to capture an exception; flag it.
                reason = "extraction missing (no kb file, no scout skip)"
            manifest["skipped"].append({
                "source_path": sp,
                "reason": reason,
            })
    # Errors will be added later from extraction logs if available.
    err_path = kb_dir / "_logs" / "errors.json"
    if err_path.is_file():
        try:
            manifest["errors"] = json.loads(err_path.read_text(encoding="utf-8"))
        except Exception:
            pass


def build_manifest(kb_dir: Path) -> dict[str, Any]:
    docs = collect_docs(kb_dir)
    total_tokens = sum((d.get("tokens_estimated") or 0) for d in docs)
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION_DOC,
        "extraction_tool": f"doc2kb@{tool_version_string()}",
        "created_at": utc_now_iso(),
        "corpus_root": None,
        "total_documents": len(docs),
        "total_tokens_estimated": total_tokens,
        "documents": docs,
        "skipped": [],
        "errors": [],
    }
    merge_scout(kb_dir, manifest)
    return manifest


def write_index_md(kb_dir: Path, manifest: dict[str, Any]) -> None:
    docs = manifest["documents"]
    n = len(docs)
    total = manifest["total_tokens_estimated"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines: list[str] = [
        "# Knowledge Base Index",
        "",
        f"{n} document(s) extracted on {today}. Estimated total: ~{total:,} tokens.",
        "",
        "## How to use",
        "",
        "1. Read `AGENTS.md` first — navigation rules, citation, trust boundary.",
        "2. Use this file as the corpus catalog: each document's headings and",
        "   warnings are listed inline below — enough to decide what is relevant.",
        "3. Open individual files in `docs/` only when relevant — do **not** bulk-load.",
        "",
        "> `manifest.json` (machine-readable: per-doc sha256, extraction_method,",
        "> token estimates in JSON) and `llms.txt` (llmstxt.org catalog for external",
        "> tools) carry no navigation info beyond this file — read them only for",
        "> programmatic filtering or sha256 provenance, not for normal navigation.",
        "",
    ]

    # By source type.
    by_type: dict[str, list[dict[str, Any]]] = {}
    for d in docs:
        by_type.setdefault(d.get("source_type") or "unknown", []).append(d)

    if by_type:
        lines.append("## By source type")
        lines.append("")
        for src_type, group in sorted(by_type.items()):
            tokens = sum((d.get("tokens_estimated") or 0) for d in group)
            lines.append(f"### {src_type} ({len(group)} document(s), ~{tokens:,} tokens)")
            lines.append("")
            for d in group:
                bits = []
                if d.get("pages"):
                    bits.append(f"{d['pages']}p")
                if d.get("slides"):
                    bits.append(f"{d['slides']} slides")
                if d.get("cells"):
                    bits.append(f"{d['cells']} cells")
                if d.get("has_outputs"):
                    bits.append("with outputs")
                if d.get("has_notes"):
                    bits.append("with notes")
                if d.get("inline_images"):
                    bits.append(f"{d['inline_images']} img")
                if d.get("tokens_estimated"):
                    bits.append(f"~{d['tokens_estimated']:,} tok")
                meta = ", ".join(bits) if bits else ""
                title = d.get("source_path") or d.get("id")
                lines.append(f"- [{title}]({d['kb_path']})"
                             + (f" — {meta}" if meta else ""))
                headings = d.get("headings") or []
                if headings:
                    lines.append("  - " + " · ".join(str(h) for h in headings))
                warnings = d.get("warnings") or []
                if warnings:
                    lines.append("  - ⚠ " + "; ".join(str(w) for w in warnings))
            lines.append("")

    # Skipped / errors.
    if manifest.get("skipped"):
        lines.append(f"## Skipped ({len(manifest['skipped'])})")
        lines.append("")
        for s in manifest["skipped"]:
            lines.append(f"- `{s.get('source_path')}` — {s.get('reason')}")
        lines.append("")
    if manifest.get("errors"):
        lines.append(f"## Errors ({len(manifest['errors'])})")
        lines.append("")
        for e in manifest["errors"]:
            lines.append(f"- `{e.get('source_path')}` — {e.get('error')}")
        lines.append("")

    (kb_dir / "INDEX.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_llms_txt(kb_dir: Path, manifest: dict[str, Any]) -> None:
    docs = manifest["documents"]
    n = len(docs)
    total = manifest["total_tokens_estimated"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tool = manifest["extraction_tool"]

    lines: list[str] = [
        "# Knowledge Base",
        f"> {n} documents, ~{total:,} tokens estimated, extracted {today} via {tool}.",
        "",
    ]
    by_type: dict[str, list[dict[str, Any]]] = {}
    for d in docs:
        by_type.setdefault(d.get("source_type") or "unknown", []).append(d)
    type_labels = {
        "pdf": "PDFs",
        "docx": "Word documents",
        "pptx": "Presentations",
        "ipynb": "Jupyter notebooks",
        "md": "Markdown",
        "txt": "Plain text",
        "html": "HTML pages",
    }
    for src_type, group in sorted(by_type.items()):
        label = type_labels.get(src_type, src_type)
        lines.append(f"## {label}")
        for d in group:
            title = d.get("source_path") or d.get("id")
            bits = []
            if d.get("pages"):
                bits.append(f"{d['pages']} pages")
            elif d.get("slides"):
                bits.append(f"{d['slides']} slides")
            elif d.get("cells"):
                bits.append(f"{d['cells']} cells")
            if d.get("tokens_estimated"):
                bits.append(f"~{d['tokens_estimated']:,} tokens")
            desc = ", ".join(bits)
            line = f"- [{title}]({d['kb_path']})"
            if desc:
                line += f": {desc}"
            lines.append(line)
        lines.append("")
    (kb_dir / "llms.txt").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_agents_md(kb_dir: Path) -> None:
    if TEMPLATE_AGENTS.is_file():
        (kb_dir / "AGENTS.md").write_text(
            TEMPLATE_AGENTS.read_text(encoding="utf-8"), encoding="utf-8"
        )
    else:
        # Fallback minimal AGENTS.md if template went missing.
        (kb_dir / "AGENTS.md").write_text(
            "# Knowledge Base\n\nRead INDEX.md — it is the corpus catalog "
            "(headings + warnings per doc). Open docs/<file>.md only as needed. "
            "manifest.json / llms.txt are for tooling, not navigation.\n",
            encoding="utf-8",
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Assemble kb manifest, INDEX, llms.txt, AGENTS.md.")
    ap.add_argument("kb_dir")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    kb_dir = Path(args.kb_dir).expanduser().resolve()
    if not kb_dir.is_dir():
        print(json.dumps({"ok": False, "reason": f"kb_dir not found: {kb_dir}"}))
        return 1

    manifest = build_manifest(kb_dir)
    (kb_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_index_md(kb_dir, manifest)
    write_llms_txt(kb_dir, manifest)
    write_agents_md(kb_dir)

    summary = {
        "ok": True,
        "kb_dir": str(kb_dir),
        "documents": manifest["total_documents"],
        "tokens_estimated": manifest["total_tokens_estimated"],
        "skipped": len(manifest.get("skipped", [])),
        "errors": len(manifest.get("errors", [])),
    }
    print(json.dumps(summary, ensure_ascii=False))
    if not args.quiet:
        print(f"  wrote manifest.json, INDEX.md, llms.txt, AGENTS.md to {kb_dir}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
