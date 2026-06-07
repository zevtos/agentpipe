#!/usr/bin/env python3
"""Validate skills/*/SKILL.md frontmatter for Codex compatibility.

Two failure modes, both silent at load time and both hard to debug after the fact,
so this validator fails fast in CI and in the pre-commit hook at
scripts/git-hooks/pre-commit:

  1. Description over Codex's 1024 UTF-8 byte limit (not characters). Codex CLI
     rejects oversized descriptions and skips the skill.
  2. YAML-hostile unquoted description. Codex parses frontmatter with a strict
     YAML loader (Ruby Psych); a plain (unquoted) scalar containing ": "
     (colon-space) or " #" (space-hash) is misparsed as a mapping/comment and
     the skill is dropped — while Claude Code's lenient parser accepts it, so
     the breakage is Codex-only and invisible locally. Fix: wrap the value in
     single quotes (double any inner ' as ''). See skills/doc2kb, skills/ultrasearch.

Exit codes:
    0  every SKILL.md is within the limit and YAML-safe
    1  at least one SKILL.md is over the limit or has a YAML-hostile description
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

# Patterns that turn an *unquoted* YAML plain scalar into something Codex's strict
# loader misparses: ": " (mapping value indicator), a trailing ":", or " #"
# (comment start). Quoted scalars are exempt.
YAML_HOSTILE_RE = re.compile(r":\s|:$|\s#")


def yaml_unsafe_reason(value: str) -> str | None:
    """Return why an unquoted description breaks Codex's YAML loader, else None."""
    if value.startswith(("'", '"')):
        return None  # quoted scalar — Codex parses it correctly
    m = YAML_HOSTILE_RE.search(value)
    if not m:
        return None
    token = m.group(0).replace("\n", "\\n")
    return (
        f"unquoted description contains YAML-hostile {token!r} — Codex's strict "
        "loader will skip this skill. Wrap the value in single quotes (double any "
        "inner ' as '')."
    )


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
        unsafe = yaml_unsafe_reason(desc)
        status = "OK  " if size <= MAX_BYTES and unsafe is None else "FAIL"
        rel = path.relative_to(ROOT) if path.is_absolute() else path
        print(f"{status} {rel}: {size} bytes (limit {MAX_BYTES})")
        if size > MAX_BYTES:
            failed += 1
        if unsafe is not None:
            sys.stderr.write(f"ERROR {rel}: {unsafe}\n")
            failed += 1

    if failed:
        sys.stderr.write(
            f"\n{failed} skill description issue(s) (over the {MAX_BYTES}-byte limit "
            "and/or YAML-hostile). Codex CLI will silently skip the affected skills "
            "at load time. Shorten the description and/or quote it as flagged above.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
