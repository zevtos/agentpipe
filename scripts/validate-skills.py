#!/usr/bin/env python3
"""Validate skills/*/SKILL.md frontmatter against Codex's 1024-byte description limit.

Codex CLI rejects skills whose YAML `description:` value exceeds 1024 UTF-8 bytes
(not characters). Skills failing the check are silently skipped at load time,
which is hard to debug after the fact — so this validator fails fast in CI and
in the pre-commit hook at scripts/git-hooks/pre-commit.

Exit codes:
    0  every SKILL.md is within the limit
    1  at least one SKILL.md is over the limit
    2  a SKILL.md is malformed (missing frontmatter or description)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

MAX_BYTES = 1024
ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
DESCRIPTION_RE = re.compile(r"^description:\s*(.*)$", re.MULTILINE)


def extract_description(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8")
    fm = FRONTMATTER_RE.match(text)
    if not fm:
        return None
    dm = DESCRIPTION_RE.search(fm.group(1))
    if not dm:
        return None
    value = dm.group(1).strip()
    if value.startswith(("|", ">")):
        sys.stderr.write(
            f"ERROR {path}: multi-line YAML description ({value[:1]}) "
            "is not supported by this validator.\n"
        )
        sys.exit(2)
    return value


def main(argv: list[str]) -> int:
    paths = [Path(p) for p in argv[1:]] if len(argv) > 1 else sorted(
        SKILLS_DIR.glob("*/SKILL.md")
    )
    if not paths:
        sys.stderr.write(f"no SKILL.md files found under {SKILLS_DIR}\n")
        return 0

    failed = 0
    for path in paths:
        desc = extract_description(path)
        if desc is None:
            sys.stderr.write(f"ERROR {path}: no YAML frontmatter or description\n")
            failed += 1
            continue
        size = len(desc.encode("utf-8"))
        status = "OK  " if size <= MAX_BYTES else "FAIL"
        rel = path.relative_to(ROOT) if path.is_absolute() else path
        print(f"{status} {rel}: {size} bytes (limit {MAX_BYTES})")
        if size > MAX_BYTES:
            failed += 1

    if failed:
        sys.stderr.write(
            f"\n{failed} skill(s) over the {MAX_BYTES}-byte description limit. "
            "Codex CLI will silently skip them at load time. Shorten the YAML "
            "description: field and move long workflow text into the body.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
