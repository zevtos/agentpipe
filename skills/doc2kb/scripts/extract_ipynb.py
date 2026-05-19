#!/usr/bin/env python3
"""
extract_ipynb.py — Phase 4 extractor for Jupyter notebooks (.ipynb).

CLI:
    extract_ipynb.py <input_ipynb> <output_md>
                     [--doc-id doc-NNN]
                     [--source-rel rel/path.ipynb]

Pipeline:
    1. Parse the notebook JSON via stdlib `json`. The .ipynb format is fully
       JSON-defined (nbformat v4) and pulling in `nbformat` would add
       jsonschema + traitlets + jupyter-core to the venv, which contradicts
       the lightweight-tier philosophy. Validation is best-effort: top-level
       `cells` array required, everything else handled defensively.
    2. For each cell emit a `## Cell N (kind)` heading + body verbatim:
       - markdown cell → source as-is.
       - code cell → fenced code block (language sniffed from `kernelspec`
         / `language_info`, allowlist-clamped, default `python`), followed
         by `### Output` with stream/result text outputs.
       - raw cell → source as-is, no code fence.
    3. Outputs kept verbatim: `stream` (stdout/stderr), `execute_result` /
       `display_data` for `text/plain` only, `error` (traceback with ANSI
       escapes stripped). `image/*` outputs become a single
       `*(image output omitted)*` placeholder per cell — same rule as DOCX
       inline images: never embed base64 in the kb.

Effect:
    Writes <output_md> with YAML frontmatter + Markdown body. Stdout
    receives a single JSON line summarizing the result.

Memory: the entire notebook is loaded into memory as bytes + decoded text
+ parsed JSON. The doc2kb `huge_file` user-decision gate (>50 MB) in scout
is the operator-visible safety net. We do not enforce a hard cap here
because the user may explicitly choose `proceed` on a large notebook and
expect it to extract; scout's `IPYNB_SCOUT_MAX_CELLS = 4000` cap exists
only because the scout phase has no user dialog yet.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from _common import (  # noqa: E402
    clean_whitespace,
    count_tokens,
    emit_failure,
    emit_success,
    sanitize_heading,
    sha256_of,
    today_iso,
    tool_version_string,
    validate_source_rel,
    write_md,
)


EXTRACTOR_NAME = "nbformat-json"
# ANSI CSI/SGR sequences. Notebooks routinely embed them in tracebacks
# (jupyter colorizes via `rich`) and progress bars (tqdm, rich). Strip in
# all text outputs — they render as garbage in Markdown and bloat tokens.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Code-fence language allowlist. Notebook metadata is attacker-controlled,
# so we clamp to a known set and fall back to "python" — the dominant
# kernel — when nothing matches.
_KNOWN_LANGS = {
    "python", "r", "julia", "javascript", "typescript", "ruby",
    "scala", "java", "kotlin", "rust", "go", "c", "cpp", "csharp",
    "bash", "sh", "powershell", "sql", "matlab", "octave", "haskell",
    "lua", "perl", "fsharp", "clojure", "elixir", "ocaml",
}
# Kernelspec.name → language fallback when `language_info.name` is missing.
# Real-world examples: R is `ir`, Haskell is `ihaskell`, Julia is
# `julia-1.x`. Without this map, an R notebook whose author saved before
# `language_info` populated would fall back to python and emit `r` source
# in a ```python fence.
_KERNELSPEC_NAME_LANG_MAP = {
    "ir": "r",
    "ihaskell": "haskell",
    "iscala": "scala",
    "ijulia": "julia",
    "iperl": "perl",
    "iocaml": "ocaml",
}


def _source_to_string(src) -> str:
    """nbformat allows `source` to be a string or a list of strings."""
    if isinstance(src, list):
        return "".join(x for x in src if isinstance(x, str))
    if isinstance(src, str):
        return src
    return ""


def _detect_language(notebook: dict) -> str:
    """Resolve a code-fence language clamped to `_KNOWN_LANGS`. Order:
    1. `metadata.language_info.name` — what nbconvert reads; standardized.
    2. `metadata.kernelspec.language` — not in the nbformat v4 spec, but
       some writers populate it; lets a hand-edited notebook be correct.
    3. `metadata.kernelspec.name` — `python3`/`ir`/`ihaskell`/`julia-1.x`;
       prefix-matched against the allowlist and the `ir → r`-style map.
    Default `python` — overwhelmingly the most common kernel and the
    safest render if the source happens to be a polyglot snippet."""
    md = notebook.get("metadata") or {}
    li = md.get("language_info") or {}
    if isinstance(li, dict):
        name = li.get("name")
        if isinstance(name, str):
            n = name.lower().strip()
            if n in _KNOWN_LANGS:
                return n
    ks = md.get("kernelspec") or {}
    if isinstance(ks, dict):
        lang = ks.get("language")
        if isinstance(lang, str):
            n = lang.lower().strip()
            if n in _KNOWN_LANGS:
                return n
        name = ks.get("name")
        if isinstance(name, str):
            n = name.lower().strip()
            if n in _KERNELSPEC_NAME_LANG_MAP:
                return _KERNELSPEC_NAME_LANG_MAP[n]
            # Common pattern: `python3`, `python2`, `julia-1.9`.
            if n.startswith("python"):
                return "python"
            if n.startswith("julia"):
                return "julia"
            if n in _KNOWN_LANGS:
                return n
    return "python"


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _fence_for(text: str, lang: str = "") -> tuple[str, str]:
    """Returns (open_fence, close_fence) using CommonMark §4.5 longest-run+1
    rule: the fence is at least 3 backticks, and exactly one more than the
    longest consecutive backtick run inside the content. Prevents a cell
    whose source happens to contain ` ``` ` from closing our wrapper early."""
    longest = 0
    cur = 0
    for ch in text:
        if ch == "`":
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
    n = max(3, longest + 1)
    bt = "`" * n
    return bt + lang, bt


def _wrap_fence(text: str, lang: str = "") -> str:
    """Wrap `text` in a Markdown fenced code block sized to survive any
    backtick runs inside it."""
    open_f, close_f = _fence_for(text, lang)
    return open_f + "\n" + text + "\n" + close_f


def _render_outputs(outputs, warnings: list[str]) -> str:
    """Renders a `### Output` section, or returns "" when nothing textual to
    show. Base64 image outputs are collapsed into one placeholder per cell.

    MIME allowlist: only `text/plain` is rendered from `execute_result` /
    `display_data` data dicts. `text/html`, `application/javascript`,
    `application/vnd.jupyter.widget-view+json`, `application/vnd.plotly.v1+json`,
    SVG, etc. are intentionally dropped — they need a sandboxed renderer
    we don't have, and rendering raw HTML/JS into the kb body would create
    a markdown/HTML injection surface for the downstream agent."""
    if not outputs:
        return ""
    chunks: list[str] = []
    n_dropped_images = 0
    for out in outputs:
        if not isinstance(out, dict):
            continue
        ot = out.get("output_type")
        if ot == "stream":
            text = out.get("text")
            if isinstance(text, list):
                text = "".join(x for x in text if isinstance(x, str))
            if not isinstance(text, str) or not text.strip():
                continue
            chunks.append(_wrap_fence(_strip_ansi(text).rstrip()))
        elif ot in ("execute_result", "display_data"):
            data = out.get("data") or {}
            if not isinstance(data, dict):
                continue
            txt = data.get("text/plain")
            if isinstance(txt, list):
                txt = "".join(x for x in txt if isinstance(x, str))
            if isinstance(txt, str) and txt.strip():
                chunks.append(_wrap_fence(_strip_ansi(txt).rstrip()))
            for key in data:
                if isinstance(key, str) and key.startswith("image/"):
                    n_dropped_images += 1
                    break
        elif ot == "error":
            ename = out.get("ename") or ""
            evalue = out.get("evalue") or ""
            tb = out.get("traceback") or []
            if isinstance(tb, list):
                tb_text = "\n".join(
                    _strip_ansi(x) for x in tb if isinstance(x, str)
                )
            else:
                tb_text = ""
            header = f"{ename}: {evalue}".strip().strip(":").strip()
            body = tb_text.rstrip()
            parts = [p for p in (header, body) if p]
            if parts:
                chunks.append(_wrap_fence("\n".join(parts)))
    if n_dropped_images:
        warnings.append(
            f"{n_dropped_images} image output(s) omitted (base64 not embedded)"
        )
        chunks.append(f"*(image output omitted: {n_dropped_images})*")
    if not chunks:
        return ""
    return "### Output\n\n" + "\n\n".join(chunks)


def extract(input_path: Path) -> tuple[str, dict, list[str]]:
    """Returns (markdown_body, frontmatter_extras, warnings)."""
    warnings: list[str] = []
    try:
        with input_path.open("rb") as fh:
            raw_bytes = fh.read()
    except Exception as e:
        raise RuntimeError(f"read failed: {e}") from e
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RuntimeError(f"notebook is not valid UTF-8: {e}") from e
    try:
        nb = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"invalid notebook JSON: {e.msg} at line {e.lineno}"
        ) from e
    if not isinstance(nb, dict):
        raise RuntimeError("notebook root is not an object")

    cells = nb.get("cells")
    if not isinstance(cells, list):
        raise RuntimeError("notebook has no `cells` array")

    lang = _detect_language(nb)  # already clamped to _KNOWN_LANGS

    extras: dict = {
        "cells": 0,
        "code_cells": 0,
        "markdown_cells": 0,
        "raw_cells": 0,
        "unknown_cells": 0,
        "has_outputs": False,
        "language": lang,
    }
    md = nb.get("metadata") or {}
    ks = md.get("kernelspec") or {}
    if isinstance(ks, dict):
        kname = ks.get("name")
        if isinstance(kname, str) and kname.strip():
            extras["kernelspec_name"] = sanitize_heading(kname, maxlen=80)

    pieces: list[str] = []
    n_cells = 0
    code_count = 0
    md_count = 0
    raw_count = 0
    unknown_count = 0
    any_outputs = False

    for idx, cell in enumerate(cells, start=1):
        if not isinstance(cell, dict):
            continue
        n_cells += 1
        ctype = cell.get("cell_type") or "unknown"
        if not isinstance(ctype, str):
            ctype = "unknown"
        src = _source_to_string(cell.get("source"))
        header = f"## Cell {idx} ({ctype})"
        body_parts: list[str] = []

        if ctype == "code":
            code_count += 1
            exec_count = cell.get("execution_count")
            if isinstance(exec_count, int):
                header = f"## Cell {idx} (code) [execution_count={exec_count}]"
            body_parts.append(header)
            body_parts.append("")
            if src.strip():
                body_parts.append(_wrap_fence(src.rstrip(), lang))
            else:
                body_parts.append("*(empty)*")
            outputs = cell.get("outputs") or []
            if isinstance(outputs, list) and outputs:
                any_outputs = True
                rendered = _render_outputs(outputs, warnings)
                if rendered:
                    body_parts.append("")
                    body_parts.append(rendered)
        elif ctype == "markdown":
            md_count += 1
            body_parts.append(header)
            body_parts.append("")
            body_parts.append(src.rstrip() if src.strip() else "*(empty)*")
        elif ctype == "raw":
            raw_count += 1
            body_parts.append(header)
            body_parts.append("")
            body_parts.append(src.rstrip() if src.strip() else "*(empty)*")
        else:
            unknown_count += 1
            warnings.append(f"cell {idx}: unknown cell_type={ctype!r}")
            body_parts.append(header)
            body_parts.append("")
            body_parts.append(src.rstrip() if src.strip() else "*(empty)*")

        pieces.append("\n".join(body_parts).rstrip())

    if pieces:
        body = "\n\n---\n\n".join(pieces).strip() + "\n"
    else:
        body = ""
    body = clean_whitespace(body)

    extras["cells"] = n_cells
    extras["code_cells"] = code_count
    extras["markdown_cells"] = md_count
    extras["raw_cells"] = raw_count
    extras["unknown_cells"] = unknown_count
    extras["has_outputs"] = any_outputs

    if n_cells == 0:
        warnings.append("notebook has no cells")

    # Headings: scan markdown-cell bodies for top-level `# ...` lines.
    # Skip lines inside code fences and the structural `## Cell N` anchors.
    headings: list[str] = []
    in_code_fence = False
    for line in body.split("\n"):
        if line.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        if line.startswith("## Cell "):
            continue
        if line.startswith("# ") and len(line) < 200:
            sanitized = sanitize_heading(line[2:])
            if sanitized:
                headings.append(sanitized)
            if len(headings) >= 10:
                break
    extras["headings"] = headings

    return body, extras, warnings


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extract Jupyter notebook → Markdown via stdlib json."
    )
    ap.add_argument("input_ipynb")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    args = ap.parse_args()

    in_path = Path(args.input_ipynb).expanduser().resolve()
    out_path = Path(args.output_md).expanduser().resolve()
    source_rel = args.source_rel or in_path.name
    try:
        source_rel = validate_source_rel(source_rel)
    except ValueError as e:
        emit_failure(f"invalid --source-rel: {e}")
        return 1

    if not in_path.is_file():
        emit_failure(f"input not found: {in_path}")
        return 1

    try:
        body, extras, warnings = extract(in_path)
    except Exception as e:
        emit_failure(f"extraction failed: {e}", extra={"input": str(in_path)})
        return 1

    fm = {
        "id": args.doc_id,
        "source": source_rel,
        "source_type": "ipynb",
        "source_sha256": sha256_of(in_path),
        "extraction_method": f"{EXTRACTOR_NAME}@{tool_version_string()}",
        "extraction_date": today_iso(),
        "cells": extras.get("cells", 0),
        "code_cells": extras.get("code_cells", 0),
        "markdown_cells": extras.get("markdown_cells", 0),
        "raw_cells": extras.get("raw_cells", 0),
        "has_outputs": extras.get("has_outputs", False),
        "language": extras.get("language", "python"),
        "headings": extras.get("headings", []),
        "tokens_estimated": count_tokens(body),
        "warnings": warnings,
    }
    if extras.get("unknown_cells"):
        fm["unknown_cells"] = extras["unknown_cells"]
    if "kernelspec_name" in extras:
        fm["kernelspec_name"] = extras["kernelspec_name"]
    write_md(out_path, fm, body)
    emit_success(out_path, body, warnings, extra={
        "cells": extras.get("cells", 0),
        "code_cells": extras.get("code_cells", 0),
        "has_outputs": extras.get("has_outputs", False),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
