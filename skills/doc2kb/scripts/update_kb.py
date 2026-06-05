#!/usr/bin/env python3
"""
update_kb.py — incremental refresh for a LIVE (self-growing) doc2kb KB.

doc2kb's main flow (scout → extract → manifest) is one-shot, built for a
fixed corpus of source documents. This script makes a KB *dynamic*: an agent
hand-authors / edits `docs/*.md` notes over time and just re-runs one command
to keep the KB consistent.

What it does (idempotent):
  1. Stamps minimal frontmatter onto any `docs/*.md` that lacks it, so
     build_manifest stops silently skipping it (collect_docs drops docs with
     no frontmatter). Auto-assigns the next free `doc-NNN` id, derives
     `headings` from the markdown, fills `source`/`source_type`/
     `extraction_method`/`warnings` defaults, and refreshes `tokens_estimated`.
     Existing frontmatter values are preserved — only missing keys are added.
  2. Regenerates manifest.json / INDEX.md / llms.txt / AGENTS.md.
  3. Self-installs `update_kb.sh` into the KB dir (once) so the agent's loop
     becomes: edit docs/ → run `./update_kb.sh`.

Run:  ensure_env.py update_kb.py <kb_dir> [--quiet]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from _common import (  # noqa: E402
    count_tokens,
    read_body,
    read_frontmatter,
    render_frontmatter,
    sanitize_heading,
    today_iso,
)
import build_manifest  # noqa: E402
import index_kb  # noqa: E402


# Canonical key order written at the top of every stamped doc.
CANONICAL_KEYS = (
    "id", "source", "source_type", "extraction_method",
    "headings", "tokens_estimated", "warnings",
)

UPDATE_SH = """#!/usr/bin/env bash
# Self-updating doc2kb KB. Edit or add docs/*.md, then run ./update_kb.sh.
# Stamps frontmatter on new docs + regenerates INDEX/manifest. Idempotent.
set -euo pipefail
SKILL="${DOC2KB_SKILL:-$HOME/.claude/skills/doc2kb}"
KB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SKILL/scripts/ensure_env.py" update_kb.py "$KB_DIR" "$@"
"""

_HEADING_RE = re.compile(r"^#{1,3}\s+(.*\S)\s*$")


def _existing_ids(docs_dir: Path) -> set[str]:
    ids: set[str] = set()
    for p in docs_dir.glob("*.md"):
        v = read_frontmatter(p).get("id")
        if v:
            ids.add(str(v))
    return ids


def _next_id(ids: set[str]) -> str:
    n = 1
    while f"doc-{n:03d}" in ids:
        n += 1
    nid = f"doc-{n:03d}"
    ids.add(nid)
    return nid


def _headings_from_body(body: str, limit: int = 12) -> list[str]:
    out: list[str] = []
    for line in body.splitlines():
        m = _HEADING_RE.match(line.strip())
        if m:
            h = sanitize_heading(m.group(1))
            if h:
                out.append(h)
        if len(out) >= limit:
            break
    return out


def stamp_docs(kb_dir: Path) -> list[str]:
    """Ensure every docs/*.md has valid frontmatter. Returns names changed."""
    docs_dir = kb_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    ids = _existing_ids(docs_dir)
    changed_files: list[str] = []

    for p in sorted(docs_dir.glob("*.md")):
        fm: dict[str, Any] = read_frontmatter(p)
        body = read_body(p)
        changed = False

        if not fm.get("id"):
            fm["id"] = _next_id(ids)
            changed = True
        if not fm.get("source"):
            fm["source"] = p.name
            changed = True
        if not fm.get("source_type"):
            fm["source_type"] = "note"
            changed = True
        if not fm.get("extraction_method"):
            fm["extraction_method"] = f"manual@{today_iso()}"
            changed = True
        if not fm.get("headings"):
            hs = _headings_from_body(body)
            if hs:
                fm["headings"] = hs
                changed = True
        if "warnings" not in fm:
            fm["warnings"] = []
            changed = True
        tok = count_tokens(body)
        if fm.get("tokens_estimated") != tok:
            fm["tokens_estimated"] = tok
            changed = True

        if changed:
            ordered: dict[str, Any] = {}
            for k in CANONICAL_KEYS:
                if k in fm:
                    ordered[k] = fm[k]
            for k, v in fm.items():
                if k not in ordered:
                    ordered[k] = v
            new_text = render_frontmatter(ordered) + "\n" + body.strip("\n") + "\n"
            p.write_text(new_text, encoding="utf-8")
            changed_files.append(p.name)

    return changed_files


def regenerate(kb_dir: Path) -> dict[str, Any]:
    manifest = build_manifest.build_manifest(kb_dir)
    (kb_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    build_manifest.write_index_md(kb_dir, manifest)
    build_manifest.write_llms_txt(kb_dir, manifest)
    build_manifest.write_agents_md(kb_dir)
    return manifest


def refresh_index_if_present(kb_dir: Path) -> bool:
    """Rebuild the BM25 search index — but only if this KB already has one.
    A live note-KB stays lean by default; once an agent opts in by building an
    index (`dkb index` / index_kb.py), every refresh keeps it current. Idempotent
    by corpus signature, so unchanged docs cost nothing."""
    if not (kb_dir / "_index.db").is_file():
        return False
    try:
        index_kb.build_index(kb_dir)
        return True
    except Exception as e:  # never let indexing break a notes refresh
        print(f"  index refresh skipped: {e}", file=sys.stderr)
        return False


def install_updater(kb_dir: Path) -> bool:
    sh = kb_dir / "update_kb.sh"
    if sh.exists():
        return False
    sh.write_text(UPDATE_SH, encoding="utf-8")
    try:
        sh.chmod(0o755)
    except OSError:
        pass
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Incrementally refresh a live doc2kb KB.")
    ap.add_argument("kb_dir")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    kb_dir = Path(args.kb_dir).expanduser().resolve()
    if not kb_dir.is_dir():
        print(json.dumps({"ok": False, "reason": f"kb_dir not found: {kb_dir}"}))
        return 1

    stamped = stamp_docs(kb_dir)
    installed = install_updater(kb_dir)
    index_refreshed = refresh_index_if_present(kb_dir)
    manifest = regenerate(kb_dir)

    summary = {
        "ok": True,
        "kb_dir": str(kb_dir),
        "stamped": stamped,
        "updater_installed": installed,
        "index_refreshed": index_refreshed,
        "documents": manifest["total_documents"],
        "tokens_estimated": manifest["total_tokens_estimated"],
    }
    print(json.dumps(summary, ensure_ascii=False))
    if not args.quiet:
        print(f"  live-kb refresh: {len(stamped)} stamped, "
              f"{manifest['total_documents']} docs in {kb_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
