#!/usr/bin/env python3
"""Validate agentpipe's internal consistency: docs-vs-disk drift and frontmatter.

agentpipe is documentation-as-code — the "product" is the agent/command markdown
files, the skill folders, and the prose that describes them. There is no runtime,
so the dominant failure mode is *silent drift*: a file is added or removed but the
counts in README.md / CLAUDE.md / docs go stale, or a frontmatter field is dropped
and the installer copies the broken artifact verbatim. This already happened once
(see CHANGELOG.md, "Corrected stale counts" — they had drifted to «2 skills / 15
docs»). This validator fails fast in the pre-commit hook (scripts/git-hooks/
pre-commit) and in CI before a release is built. Sibling of validate-skills.py;
same exit-code contract.

Checks:
    A  count claims in prose (README.md, CLAUDE.md, docs/installation.md) match disk
    B  agent frontmatter — name/description/tools/model present, name==filename,
       model in {opus,sonnet}, tools subset of the allowed set, description has
       "MUST BE USED"
    C  command frontmatter — description present, body has @CLAUDE.md, $ARGUMENTS
       present iff argument-hint present
    D  skill folder shape — SKILL.md + LICENSE present, frontmatter name==folder
    E  installer flag parity — long-option set of install.sh == install.ps1
       (WARNING only in v1: the two installer DSLs are parsed by regex and the
       check is the most brittle; a mismatch warns but does not fail the build)
    F  installer supply-chain pinning (FAIL) — no `@latest` or moving git ref
       (raw.githubusercontent.com/<repo>/(main|master)/) anywhere in install.sh /
       install.ps1; every external install must pin an exact version / commit SHA

Exit codes (identical contract to validate-skills.py):
    0  every check passed (Check E warnings do not change this)
    1  at least one consistency check (A-D) failed — drift detected
    2  the validator could not run a check (malformed/unreadable required input)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- Disk truth: the single source of truth for every count claim. ------------
PRODUCT_GLOBS = {
    "agents": "agents/*.md",
    "commands": "commands/*.md",
    "skills": "skills/*/SKILL.md",
    "research": "research/*.md",
}

# Prose files whose count claims are validated against disk. CHANGELOG.md and
# research/ are intentionally excluded: the changelog quotes historical (stale)
# counts on purpose, and research/ holds reference prose, not claims about the repo.
COUNT_FILES = ["README.md", "CLAUDE.md", "docs/installation.md"]

# Each pattern captures the claimed integer and maps a (synonymous) category noun
# to a disk category. Up to two adjective words (letters/hyphens, e.g.
# "multi-agent") may sit between the number and the noun. Anchoring on the nouns
# — not on bare integers — keeps "30 seconds" / "1024 bytes" from matching.
_ADJ = r"(?:[A-Za-z-]+\s+){0,2}"
COUNT_PATTERNS = [
    (re.compile(rf"(\d+)\s+{_ADJ}agents?\b", re.I), "agents"),
    (re.compile(rf"(\d+)\s+{_ADJ}commands?\b", re.I), "commands"),
    (re.compile(rf"(\d+)\s+{_ADJ}workflows?\b", re.I), "commands"),
    (re.compile(rf"(\d+)\s+{_ADJ}skills?\b", re.I), "skills"),
    (re.compile(rf"(\d+)\s+{_ADJ}research\b", re.I), "research"),
    (re.compile(rf"(\d+)\s+{_ADJ}reference\s+(?:documents?|docs?)\b", re.I), "research"),
]

ALLOWED_TOOLS = {"Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebFetch", "WebSearch"}
AGENT_MODELS = {"opus", "sonnet"}
REQUIRED_AGENT_FIELDS = ("name", "description", "tools", "model")

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)

# PowerShell PascalCase params whose kebab-case form differs from the install.sh flag.
PS_FLAG_ALIASES = {"show-version": "version"}


def field(block: str, name: str) -> str | None:
    """Return the single-line value of a frontmatter field, or None if absent.

    Exits 2 on a multi-line YAML value (|/>) — same bailout as validate-skills.py.
    """
    m = re.search(rf"^{re.escape(name)}:\s*(.*)$", block, re.MULTILINE)
    if not m:
        return None
    value = m.group(1).strip()
    if value.startswith(("|", ">")):
        sys.stderr.write(
            f"ERROR multi-line YAML value for '{name}:' is not supported by this "
            "validator.\n"
        )
        sys.exit(2)
    return value


def frontmatter(path: Path) -> str:
    """Return the frontmatter block of a markdown file; exit 2 if it has none."""
    fm = FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    if not fm:
        sys.stderr.write(f"ERROR {rel(path)}: no YAML frontmatter\n")
        sys.exit(2)
    return fm.group(1)


def body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    fm = FRONTMATTER_RE.match(text)
    return text[fm.end():] if fm else text


def rel(path: Path) -> Path:
    return path.relative_to(ROOT) if path.is_absolute() else path


# --- Check A: count claims vs disk --------------------------------------------
def check_counts(disk: dict[str, int], fails: list[str]) -> None:
    for name in COUNT_FILES:
        path = ROOT / name
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # One number resolves to one category. A phrasing like "5 research
            # agents" matches both the agents and research patterns at the same
            # digit; first pattern (in COUNT_PATTERNS order) wins so the claim is
            # not double-counted into two categories.
            claimed_at: set[int] = set()
            for pattern, category in COUNT_PATTERNS:
                for m in pattern.finditer(line):
                    if m.start(1) in claimed_at:
                        continue
                    claimed_at.add(m.start(1))
                    claimed = int(m.group(1))
                    actual = disk[category]
                    if claimed != actual:
                        fails.append(
                            f"FAIL {name}:{lineno}: claims {claimed} {category}, "
                            f"disk has {actual} — \"{m.group(0).strip()}\""
                        )


# --- Check B: agent frontmatter -----------------------------------------------
def check_agents(fails: list[str]) -> None:
    for path in sorted((ROOT / "agents").glob("*.md")):
        block = frontmatter(path)
        name = rel(path)
        for f in REQUIRED_AGENT_FIELDS:
            if field(block, f) is None:
                fails.append(f"FAIL {name}: missing frontmatter field '{f}'")
        fm_name = field(block, "name")
        if fm_name is not None and fm_name != path.stem:
            fails.append(f"FAIL {name}: name '{fm_name}' != filename '{path.stem}'")
        model = field(block, "model")
        if model is not None and model not in AGENT_MODELS:
            fails.append(f"FAIL {name}: model '{model}' not in {sorted(AGENT_MODELS)}")
        tools = field(block, "tools")
        if tools is not None:
            unknown = [t.strip() for t in tools.split(",") if t.strip()
                       and t.strip() not in ALLOWED_TOOLS]
            if unknown:
                fails.append(f"FAIL {name}: unknown tool(s) {unknown}")
        desc = field(block, "description")
        if desc is not None and "MUST BE USED" not in desc:
            fails.append(f"FAIL {name}: description missing 'MUST BE USED' trigger")


# --- Check C: command frontmatter ---------------------------------------------
def check_commands(fails: list[str]) -> None:
    for path in sorted((ROOT / "commands").glob("*.md")):
        block = frontmatter(path)
        name = rel(path)
        if not field(block, "description"):
            fails.append(f"FAIL {name}: missing or empty 'description'")
        text_body = body(path)
        if "@CLAUDE.md" not in text_body:
            fails.append(f"FAIL {name}: body does not load @CLAUDE.md")
        has_hint = field(block, "argument-hint") is not None
        has_args = "$ARGUMENTS" in text_body
        if has_hint and not has_args:
            fails.append(f"FAIL {name}: argument-hint set but $ARGUMENTS absent in body")
        if has_args and not has_hint:
            fails.append(f"FAIL {name}: $ARGUMENTS used but argument-hint absent")


# --- Check D: skill folder shape ----------------------------------------------
def check_skills(fails: list[str]) -> None:
    for skill_md in sorted((ROOT / "skills").glob("*/SKILL.md")):
        folder = skill_md.parent
        if not (folder / "LICENSE").exists():
            fails.append(f"FAIL skills/{folder.name}: missing LICENSE")
        fm_name = field(frontmatter(skill_md), "name")
        if fm_name is not None and fm_name != folder.name:
            fails.append(
                f"FAIL {rel(skill_md)}: name '{fm_name}' != folder '{folder.name}'"
            )
    for folder in sorted(p for p in (ROOT / "skills").iterdir() if p.is_dir()):
        if not (folder / "SKILL.md").exists():
            fails.append(f"FAIL skills/{folder.name}: missing SKILL.md")


# --- Check E: installer flag parity (WARNING only) ----------------------------
def sh_flags() -> set[str]:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    block = re.search(r'case\s+"\$1"\s+in(.*?)\n\s*esac', text, re.DOTALL)
    if not block:
        return set()
    arm = re.compile(r"^\s*--([a-z][a-z-]+)(?:=\*)?(?:\|-\w)?\)", re.MULTILINE)
    return set(arm.findall(block.group(1)))


def ps_flags() -> set[str]:
    text = (ROOT / "install.ps1").read_text(encoding="utf-8")
    block = re.search(r"param\((.*?)\n\)", text, re.DOTALL)
    if not block:
        return set()
    names = re.findall(r"\[(?:switch|string)\]\$(\w+)", block.group(1))
    flags = set()
    for n in names:
        kebab = re.sub(r"(?<!^)(?=[A-Z])", "-", n).lower()
        flags.add(PS_FLAG_ALIASES.get(kebab, kebab))
    return flags


# --- Check F: installer supply-chain pinning (FAIL) ----------------------------
# The installers must never fetch/exec an external artifact from a moving ref: no
# `@latest` (npm) and no raw.githubusercontent.com/<repo>/(main|master)/ (git
# branch). Every external install must pin an exact version or commit SHA so a
# poisoned upstream cannot reach users who already accepted the gate. Applies to
# the whole installer text (exec + help + hints) so a stray unpinned string in a
# "run this later" message can't drift back to recommending `@latest`.
_UNPINNED_RE = re.compile(r"@latest|raw\.githubusercontent\.com/[^\s\"']*/(?:main|master)/")


def check_installer_pinning(fails: list[str]) -> None:
    for fname in ("install.sh", "install.ps1"):
        path = ROOT / fname
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = _UNPINNED_RE.search(line)
            if m:
                fails.append(
                    f"FAIL {fname}:{lineno}: unpinned external ref '{m.group(0)}' — "
                    f"pin an exact version/commit SHA (supply-chain). "
                    f"\"{line.strip()[:80]}\""
                )


def check_installer_parity(warns: list[str]) -> None:
    sh, ps = sh_flags(), ps_flags()
    if not sh or not ps:
        warns.append(
            "WARN installer parity skipped: parsed 0 flags from "
            f"{'install.sh' if not sh else 'install.ps1'} — the parser may need "
            "updating after an installer refactor."
        )
        return
    only_sh = sorted(sh - ps)
    only_ps = sorted(ps - sh)
    if only_sh:
        warns.append(f"WARN flags in install.sh but not install.ps1: {only_sh}")
    if only_ps:
        warns.append(f"WARN flags in install.ps1 but not install.sh: {only_ps}")


def main() -> int:
    disk = {cat: len(sorted(ROOT.glob(g))) for cat, g in PRODUCT_GLOBS.items()}
    for cat, n in disk.items():
        if n == 0:
            sys.stderr.write(f"ERROR no files found for '{cat}' ({PRODUCT_GLOBS[cat]})\n")
            return 2

    fails: list[str] = []
    warns: list[str] = []
    check_counts(disk, fails)
    check_agents(fails)
    check_commands(fails)
    check_skills(fails)
    check_installer_pinning(fails)
    check_installer_parity(warns)

    for w in warns:
        sys.stderr.write(w + "\n")
    if fails:
        for f in fails:
            sys.stderr.write(f + "\n")
        sys.stderr.write(
            f"\n{len(fails)} consistency check(s) failed. Fix the drift above "
            "(update the prose to match disk, or correct the frontmatter).\n"
        )
        return 1

    counts = ", ".join(f"{n} {c}" for c, n in disk.items())
    print(f"OK  repo consistency: {counts}; agent/command/skill frontmatter valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
