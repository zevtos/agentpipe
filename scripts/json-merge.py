#!/usr/bin/env python3
"""
agentpipe: deep-merge a JSON literal into a target file, preserving user keys.

Usage:
    python3 scripts/json-merge.py [--list-union path]... <target-path> '<json-literal>'

Options:
    --list-union <dotted.path>   Repeatable. For the given dotted JSON path, when
                                 both base and overlay hold lists at that path,
                                 merge as set-union (preserve overlay-first order,
                                 append base-only items afterward, dedupe). Without
                                 this flag, lists overwrite as scalars do.

Behavior:
    - Reads <target-path>, or treats as {} if missing.
    - Deep-merges the literal (objects merge recursively, scalars overwrite).
    - Lists overwrite by default, OR set-union for paths listed in --list-union.
    - Atomically replaces the target (temp file + rename).
    - On parse error of the existing file: bails non-zero and leaves the original untouched.

Exit codes:
    0  merged or already current (idempotent)
    1  parse error in target or argv literal
    2  bad invocation
"""
from __future__ import annotations

import json
import os
import sys
import tempfile


def _list_union(base_list: list, overlay_list: list) -> list:
    """Set-union with overlay-first order; falls back to identity for unhashable items."""
    out: list = []
    seen_hashable: set = set()
    seen_unhashable: list = []

    def add(item):
        try:
            if item in seen_hashable:
                return
            seen_hashable.add(item)
        except TypeError:
            # unhashable (dict/list) — linear-scan dedupe
            if item in seen_unhashable:
                return
            seen_unhashable.append(item)
        out.append(item)

    for item in overlay_list:
        add(item)
    for item in base_list:
        add(item)
    return out


def deep_merge(base: dict, overlay: dict, list_union_paths: set, path: str = "") -> dict:
    out = dict(base)
    for key, value in overlay.items():
        sub_path = f"{path}.{key}" if path else key
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = deep_merge(out[key], value, list_union_paths, sub_path)
        elif (
            key in out
            and isinstance(out[key], list)
            and isinstance(value, list)
            and sub_path in list_union_paths
        ):
            out[key] = _list_union(out[key], value)
        else:
            out[key] = value
    return out


def main(argv: list[str]) -> int:
    args = argv[1:]
    list_union_paths: set = set()

    while args and args[0] == "--list-union":
        if len(args) < 2:
            print(f"usage: {argv[0]} [--list-union path]... <target-path> <json-literal>", file=sys.stderr)
            return 2
        list_union_paths.add(args[1])
        args = args[2:]

    if len(args) != 2:
        print(f"usage: {argv[0]} [--list-union path]... <target-path> <json-literal>", file=sys.stderr)
        return 2

    target_path = args[0]
    try:
        overlay = json.loads(args[1])
    except json.JSONDecodeError as exc:
        print(f"json-merge: invalid JSON literal: {exc}", file=sys.stderr)
        return 1
    if not isinstance(overlay, dict):
        print("json-merge: literal must be a JSON object", file=sys.stderr)
        return 1

    base: dict = {}
    if os.path.exists(target_path):
        try:
            with open(target_path, "r", encoding="utf-8") as fh:
                content = fh.read()
            if content.strip():
                base = json.loads(content)
            if not isinstance(base, dict):
                print(
                    f"json-merge: {target_path} is not a JSON object; refusing to merge",
                    file=sys.stderr,
                )
                return 1
        except json.JSONDecodeError as exc:
            print(
                f"json-merge: {target_path} has invalid JSON: {exc}; leaving unchanged",
                file=sys.stderr,
            )
            return 1

    merged = deep_merge(base, overlay, list_union_paths)

    if merged == base and os.path.exists(target_path):
        return 0

    target_dir = os.path.dirname(os.path.abspath(target_path)) or "."
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".json-merge.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_path, target_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
