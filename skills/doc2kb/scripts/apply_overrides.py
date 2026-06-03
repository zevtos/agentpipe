#!/usr/bin/env python3
"""
apply_overrides.py — declaratively patch _scout.json from _overrides.json.

Phase 3.5 (between Decide and Extract). Instead of hand-editing the
scout JSON to force a couple of files through a different extractor (e.g.
`mineru`) or to attach per-file MinerU settings, you author a small
`<kb_dir>/_overrides.json` and run this. The patch is deterministic,
validated before AND after, and written atomically — it can never leave a
half-written or schema-broken `_scout.json` behind.

CLI:
    apply_overrides.py <kb_dir> [--config <path>] [--dry-run] [--allow-unmatched]

Default config: <kb_dir>/_overrides.json

Config schema (stdlib json — no new deps):
    {
      "version": 1,
      "overrides": [
        { "match": "papers/*.pdf",          // glob on source_path, OR exact
          "strategy": "mineru",             //   doc-id, OR exact source_path
          "mineru": { "backend": "vlm-auto-engine",
                      "lang": "cyrillic",
                      "keep_raw": true },
          "popo": true,                     // per-file Popo opt-in/out (overrides env)
          "note": "scanned scans, send to VLM" },
        { "match": "doc-012", "strategy": "skip" }
      ]
    }

Matching: a `match` shaped like `doc-NNN` is treated as an exact id; anything
else is matched against `source_path` exactly, then as an fnmatch glob. Rules
apply in listed order, last-wins per field.

On a runnable strategy the file's `action_required` is nulled (the override IS
the Phase-3 decision). A `mineru` block is stored structurally on the file
entry (extract_corpus renders the CLI flags from it — never raw arg strings)
and is inert unless the resolved strategy is `mineru` (a warning is emitted).

Exit codes:
    0 — applied (or dry-run) cleanly.
    1 — a rule matched 0 files (likely a typo) and --allow-unmatched not set,
        OR the post-apply scout failed re-validation. Nothing was written.
    2 — _scout.json / config missing, unreadable, or invalid. Nothing written.
"""
from __future__ import annotations

import argparse
import copy
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

from _common import log as _common_log

# Single-source the canonical strategy / backend sets so this script can never
# drift from the dispatcher and the mineru extractor.
from extract_corpus import DISPATCH, NON_RUNNABLE
from extract_pdf_mineru import SUPPORTED_BACKENDS


KNOWN_STRATEGIES = set(DISPATCH) | set(NON_RUNNABLE)
_DOC_ID_RE = re.compile(r"doc-\d{3,}$")
_LANG_RE = re.compile(r"^[a-z]{2,}$")
_ALLOWED_RULE_KEYS = {"match", "strategy", "mineru", "popo", "note"}
_ALLOWED_MINERU_KEYS = {"backend", "lang", "keep_raw"}


def log(msg: str) -> None:
    _common_log(msg, prefix="doc2kb overrides")


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False))


class ConfigError(Exception):
    """A validation problem in _overrides.json or the post-apply scout."""


def _validate_config(cfg: object) -> list[dict]:
    if not isinstance(cfg, dict):
        raise ConfigError("config root must be a JSON object")
    version = cfg.get("version", 1)
    if version != 1:
        raise ConfigError(f"unsupported config version {version!r} (expected 1)")
    overrides = cfg.get("overrides")
    if not isinstance(overrides, list):
        raise ConfigError("'overrides' must be a list")
    for i, rule in enumerate(overrides):
        loc = f"overrides[{i}]"
        if not isinstance(rule, dict):
            raise ConfigError(f"{loc} must be an object")
        unknown = set(rule) - _ALLOWED_RULE_KEYS
        if unknown:
            raise ConfigError(f"{loc} has unknown key(s): {sorted(unknown)} "
                              f"(allowed: {sorted(_ALLOWED_RULE_KEYS)})")
        match = rule.get("match")
        if not isinstance(match, str) or not match.strip():
            raise ConfigError(f"{loc}.match must be a non-empty string")
        strat = rule.get("strategy")
        if strat is not None and strat not in KNOWN_STRATEGIES:
            raise ConfigError(f"{loc}.strategy={strat!r} not in "
                              f"{sorted(KNOWN_STRATEGIES)}")
        mineru = rule.get("mineru")
        if mineru is not None:
            if not isinstance(mineru, dict):
                raise ConfigError(f"{loc}.mineru must be an object")
            uk = set(mineru) - _ALLOWED_MINERU_KEYS
            if uk:
                raise ConfigError(f"{loc}.mineru has unknown key(s): {sorted(uk)} "
                                  f"(allowed: {sorted(_ALLOWED_MINERU_KEYS)})")
            backend = mineru.get("backend")
            if backend is not None and backend not in SUPPORTED_BACKENDS:
                raise ConfigError(f"{loc}.mineru.backend={backend!r} not in "
                                  f"{list(SUPPORTED_BACKENDS)}")
            lang = mineru.get("lang")
            if lang is not None and not (isinstance(lang, str) and _LANG_RE.match(lang)):
                raise ConfigError(f"{loc}.mineru.lang={lang!r} must match "
                                  f"{_LANG_RE.pattern}")
            keep_raw = mineru.get("keep_raw")
            if keep_raw is not None and not isinstance(keep_raw, bool):
                raise ConfigError(f"{loc}.mineru.keep_raw must be a boolean")
        popo = rule.get("popo")
        if popo is not None and not isinstance(popo, bool):
            raise ConfigError(f"{loc}.popo must be a boolean")
    return overrides


def _matches(rule_match: str, file_entry: dict) -> bool:
    fid = file_entry.get("id") or ""
    spath = file_entry.get("source_path") or ""
    if _DOC_ID_RE.match(rule_match):
        return rule_match == fid
    if rule_match == spath:
        return True
    return fnmatch.fnmatch(spath, rule_match)


def _fingerprint(f: dict) -> tuple:
    return (
        f.get("extraction_strategy"),
        f.get("action_required"),
        json.dumps(f.get("mineru"), sort_keys=True, ensure_ascii=False),
        f.get("popo"),
    )


def _apply(scout: dict, overrides: list[dict]) -> tuple[list[int], list[dict]]:
    """Mutate `scout` in place. Returns (per-rule hit counts, changed entries)."""
    files = scout.get("files") or []
    rule_hits = [0] * len(overrides)
    changed: list[dict] = []
    for f in files:
        before = _fingerprint(f)
        for ri, rule in enumerate(overrides):
            if not _matches(rule["match"], f):
                continue
            rule_hits[ri] += 1
            if rule.get("strategy") is not None:
                f["extraction_strategy"] = rule["strategy"]
                # The override IS the Phase-3 decision — clear the gate.
                f["action_required"] = None
            if rule.get("mineru") is not None:
                block = dict(f.get("mineru") or {})
                block.update(rule["mineru"])
                f["mineru"] = block
            if "popo" in rule:
                f["popo"] = rule["popo"]
        if _fingerprint(f) != before:
            changed.append({
                "id": f.get("id"),
                "source_path": f.get("source_path"),
                "extraction_strategy": f.get("extraction_strategy"),
                "mineru": f.get("mineru"),
                "popo": f.get("popo"),
            })
    return rule_hits, changed


def _validate_scout_shape(scout: object) -> None:
    if not isinstance(scout, dict):
        raise ConfigError("scout root is not an object")
    files = scout.get("files")
    if not isinstance(files, list):
        raise ConfigError("scout has no 'files' list after apply")
    for f in files:
        if not isinstance(f, dict):
            raise ConfigError("a scout file entry is not an object")
        fid = f.get("id")
        if not (isinstance(fid, str) and _DOC_ID_RE.match(fid)):
            raise ConfigError(f"file id {fid!r} is not a valid doc-NNN")
        if not isinstance(f.get("source_path"), str):
            raise ConfigError(f"file {fid} source_path is not a string")
        if not isinstance(f.get("extraction_strategy"), str):
            raise ConfigError(f"file {fid} extraction_strategy is not a string")


def _atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Deterministically patch _scout.json from _overrides.json "
                    "(per-file strategy + MinerU settings + Popo flag)."
    )
    ap.add_argument("kb_dir", help="kb directory containing _scout.json")
    ap.add_argument("--config", default=None,
                    help="overrides config (default: <kb_dir>/_overrides.json)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the diff, write nothing")
    ap.add_argument("--allow-unmatched", action="store_true",
                    help="downgrade 0-match rules from a hard error to a warning")
    args = ap.parse_args()

    kb = Path(args.kb_dir).expanduser().resolve()
    scout_path = kb / "_scout.json"
    cfg_path = (Path(args.config).expanduser().resolve()
                if args.config else kb / "_overrides.json")

    if not scout_path.is_file():
        _emit({"ok": False, "reason": f"_scout.json not found at {scout_path} "
                                       "— run Phase 2 (scout) first"})
        return 2
    if not cfg_path.is_file():
        _emit({"ok": False, "reason": f"overrides config not found at {cfg_path}"})
        return 2
    try:
        scout = json.loads(scout_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _emit({"ok": False, "reason": f"_scout.json unreadable: {e}"})
        return 2
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        overrides = _validate_config(cfg)
    except (OSError, json.JSONDecodeError) as e:
        _emit({"ok": False, "reason": f"overrides config unreadable: {e}"})
        return 2
    except ConfigError as e:
        _emit({"ok": False, "reason": f"invalid overrides config: {e}"})
        return 2

    patched = copy.deepcopy(scout)
    rule_hits, changed = _apply(patched, overrides)

    inert = [{"id": f.get("id"), "source_path": f.get("source_path")}
             for f in (patched.get("files") or [])
             if f.get("mineru") and f.get("extraction_strategy") != "mineru"]
    unmatched = [overrides[i]["match"] for i, n in enumerate(rule_hits) if n == 0]

    try:
        _validate_scout_shape(patched)
    except ConfigError as e:
        _emit({"ok": False,
               "reason": f"refusing to write — post-apply scout invalid: {e}"})
        return 1

    for item in inert:
        log(f"WARNING: {item['source_path']} has a mineru block but strategy "
            "!= mineru — block is inert until strategy is mineru")

    blocked_by_unmatched = bool(unmatched) and not args.allow_unmatched
    if unmatched:
        log(f"WARNING: {len(unmatched)} override rule(s) matched 0 files: {unmatched}"
            + ("" if args.allow_unmatched else " (pass --allow-unmatched to proceed)"))

    wrote = False
    if not args.dry_run and not blocked_by_unmatched:
        _atomic_write_json(scout_path, patched)
        wrote = True

    _emit({
        "ok": not blocked_by_unmatched,
        "kb_root": str(kb),
        "config": str(cfg_path),
        "scout": str(scout_path),
        "dry_run": bool(args.dry_run),
        "wrote": wrote,
        "rules": len(overrides),
        "rule_hits": rule_hits,
        "unmatched_rules": unmatched,
        "inert_mineru_blocks": inert,
        "changed_count": len(changed),
        "changed": changed,
    })
    return 1 if blocked_by_unmatched else 0


if __name__ == "__main__":
    sys.exit(main())
