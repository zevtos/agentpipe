#!/usr/bin/env python3
"""
agentpipe: strip every sound-hook entry (Stop and Notification) from a Claude
Code settings.json or Codex hooks.json. Used by `install.sh --clean-sound-hooks`
to undo earlier `--with-sound-hooks` / `--with-notification-sound` merges
without touching unrelated hooks (gost-validation, user customs).

Usage:
    python3 scripts/clean-sound-hooks.py <settings-path>

Behavior:
    - Loads <settings-path>; treats missing or empty as a no-op.
    - For each hook entry under hooks.Stop and hooks.Notification, drops
      inner `hooks` items whose `command` matches a sound-cue pattern.
    - Drops a wrapper whose `hooks` list becomes empty.
    - Drops Stop/Notification keys whose list becomes empty.
    - Drops the top-level `hooks` object if it becomes empty.
    - Atomically rewrites <settings-path> only when something changed.
    - Prints the count of removed inner-hook entries to stdout.

Patterns recognised as sound cues:
    afplay                 (macOS — Hero.aiff / Glass.aiff)
    paplay                 (Linux — freedesktop complete.oga)
    [console]::beep        (Windows native PowerShell beep)
    powershell(.exe) ... beep  (WSL → Windows beep via interop)

Exit codes:
    0  success (whether or not anything was removed)
    1  read/parse failure
    2  bad invocation
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile

# Compiled once. `re.IGNORECASE` covers `Powershell.EXE`, `[Console]::Beep`, etc.
_SOUND_PATTERN = re.compile(
    r"(?:\bafplay\b|\bpaplay\b|\[console\]::beep|powershell(?:\.exe)?\b[^|;&]*\bbeep\b)",
    re.IGNORECASE,
)


def _is_sound_command(cmd: str) -> bool:
    return bool(_SOUND_PATTERN.search(cmd or ""))


def _filter_event_list(entries: list) -> tuple[list, int]:
    """Return (filtered-entries, count-of-removed-inner-hooks)."""
    out: list = []
    removed = 0
    for entry in entries:
        if not isinstance(entry, dict):
            out.append(entry)
            continue
        inner = entry.get("hooks", [])
        if not isinstance(inner, list):
            out.append(entry)
            continue
        kept = [
            h for h in inner
            if not (isinstance(h, dict) and _is_sound_command(h.get("command", "")))
        ]
        removed += len(inner) - len(kept)
        if kept:
            new_entry = dict(entry)
            new_entry["hooks"] = kept
            out.append(new_entry)
        # else: drop the wrapper entirely
    return out, removed


def main(argv: list) -> int:
    if len(argv) != 2:
        print("usage: clean-sound-hooks.py <settings-path>", file=sys.stderr)
        return 2

    path = argv[1]
    if not os.path.exists(path):
        print(0)
        return 0

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as exc:
        print(f"read failed: {exc}", file=sys.stderr)
        return 1

    if not raw.strip():
        print(0)
        return 0

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"parse failed: {exc}", file=sys.stderr)
        return 1

    if not isinstance(data, dict):
        print(0)
        return 0

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        print(0)
        return 0

    total_removed = 0
    for event in ("Stop", "Notification"):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        filtered, removed = _filter_event_list(entries)
        total_removed += removed
        if filtered:
            hooks[event] = filtered
        else:
            del hooks[event]

    if not hooks:
        del data["hooks"]

    if total_removed > 0:
        # Atomic rewrite
        dirpath = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=dirpath)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp, path)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            print(f"write failed: {exc}", file=sys.stderr)
            return 1

    print(total_removed)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
