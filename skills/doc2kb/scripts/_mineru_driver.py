#!/usr/bin/env python3
"""
_mineru_driver.py — MinerU 4 worker of doc2kb's opt-in `mineru` tier.

Runs ONLY under the tier's isolated venv (`<state>/mineru/venv`, built by
`ensure_env.py --tier mineru`): it imports `mineru`, which never lives in the
lightweight venv. Self-contained on purpose — stdlib + mineru, no `_common`.
extract_pdf_mineru.py (lightweight venv) runs it as a subprocess with `-I`.

MinerU 4 replaced the 3.x `mineru -p … -b … -l …` CLI and its per-backend
output tree with the `mineru.parser.parse(path, tier=, ocr_mode=, page_range=)`
SDK; this worker is the only code that touches that SDK.

CLI:
    _mineru_driver.py parse <pdf> --out DIR [--tier standard|advanced|basic]
                      [--ocr-mode ocr|auto|txt] [--pages 2,18-19,35]
                      [--models-dir D]
        → DIR/markdown.md, middle_json.json, structured_content.json, images/
          (ParseResult.save) plus DIR/content_list.json — Content List V1
          rendered from the SAVED middle JSON, so every img_path is
          `images/<name>` rather than an inline data: URI — and one JSON line
          on stdout: {"ok", "mineru_version", "tier", "ocr_mode",
          "page_range", "page_idx": [...]}. `page_idx` values are 0-based
          ORIGINAL page indices even with --pages (MinerU keeps source
          indices), so callers never remap.
    _mineru_driver.py setup --models-dir D [--tier standard]
        → downloads the tier's models (`mineru-kit models download`) into D.
          ensure_env.py calls this once per requirements hash.
    _mineru_driver.py version
        → {"ok": true, "mineru_version": "4.2.3"}

Exit codes: 0 ok, 1 failure (message in the JSON line), 2 mineru not importable.

--models-dir sets MINERU_MODEL_BASE_DIR and MINERU_HOME (its parent) before
mineru is imported, so weights and MinerU's config.yaml stay under
<state>/mineru/ and leave with the tier; without it MinerU uses its own
default (~/.mineru). MINERU_TELEMETRY_DISABLED=1 is always set.

ocr_mode default is "ocr": the VLM reads every block. "auto" takes text from a
PDF's text layer where one exists — measured on a LaTeX lab report it kept
line-break hyphenation ("вычисля- лось") and collapsed a 6-column table into
2 columns, which is exactly what page-patching is meant to repair.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

TIERS = ("standard", "advanced", "basic")
OCR_MODES = ("ocr", "auto", "txt")


def _out(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _set_home(models_dir: str | None) -> None:
    os.environ.setdefault("MINERU_TELEMETRY_DISABLED", "1")
    if models_dir:
        d = Path(models_dir).expanduser().resolve()
        d.mkdir(parents=True, exist_ok=True)
        os.environ["MINERU_MODEL_BASE_DIR"] = str(d)
        os.environ["MINERU_HOME"] = str(d.parent)


def _version() -> str:
    from importlib.metadata import version
    return version("mineru")


def cmd_parse(a: argparse.Namespace) -> int:
    _set_home(a.models_dir)
    try:
        from mineru.parser import ParseResult, parse
        from mineru.parser.writer import FileBasedDataWriter
        from mineru.render import render_content_list
    except ImportError as e:
        _out({"ok": False, "error": f"mineru is not importable in {sys.executable}: {e}"})
        return 2
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        result = parse(Path(a.pdf), tier=a.tier, ocr_mode=a.ocr_mode,
                       page_range=a.pages or "all")
        result.save(FileBasedDataWriter(str(out)))
        saved = ParseResult.from_json((out / "middle_json.json").read_text(encoding="utf-8"))
        content_list = render_content_list(saved.middle_json)
    except Exception as e:  # noqa: BLE001 — surface any SDK failure as one JSON line
        _out({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return 1
    (out / "content_list.json").write_text(
        json.dumps(content_list, ensure_ascii=False, indent=1), encoding="utf-8")
    page_idx = sorted({e["page_idx"] for e in content_list
                       if isinstance(e, dict) and isinstance(e.get("page_idx"), int)})
    _out({"ok": True, "mineru_version": _version(), "tier": a.tier,
          "ocr_mode": a.ocr_mode, "page_range": a.pages or "all", "page_idx": page_idx})
    return 0


def cmd_setup(a: argparse.Namespace) -> int:
    _set_home(a.models_dir)
    kit = Path(sys.executable).with_name("mineru-kit.exe" if os.name == "nt" else "mineru-kit")
    if not kit.exists():
        _out({"ok": False, "error": f"mineru-kit not found next to {sys.executable}"})
        return 2
    rc = subprocess.run([str(kit), "models", "download", "--tier", a.tier]).returncode
    _out({"ok": rc == 0, "models_dir": os.environ.get("MINERU_MODEL_BASE_DIR"), "tier": a.tier})
    return 0 if rc == 0 else 1


def cmd_version(_a: argparse.Namespace) -> int:
    try:
        _out({"ok": True, "mineru_version": _version()})
    except Exception as e:  # noqa: BLE001
        _out({"ok": False, "error": str(e)})
        return 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="MinerU 4 worker for doc2kb's mineru tier")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse")
    p.add_argument("pdf")
    p.add_argument("--out", required=True)
    p.add_argument("--tier", choices=TIERS, default="standard")
    p.add_argument("--ocr-mode", choices=OCR_MODES, default="ocr")
    p.add_argument("--pages", default=None, help="1-based list, e.g. 2,18-19,35")
    p.add_argument("--models-dir", default=None)
    p.set_defaults(fn=cmd_parse)
    s = sub.add_parser("setup")
    s.add_argument("--models-dir", required=True)
    s.add_argument("--tier", choices=TIERS, default="standard")
    s.set_defaults(fn=cmd_setup)
    v = sub.add_parser("version")
    v.set_defaults(fn=cmd_version)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
