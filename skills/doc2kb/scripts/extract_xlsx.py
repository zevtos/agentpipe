#!/usr/bin/env python3
"""
extract_xlsx.py — Phase 4 extractor for Excel workbooks (.xlsx / .xlsm).

CLI:
    extract_xlsx.py <input_xlsx> <output_md>
                    [--doc-id doc-NNN]
                    [--source-rel relative/path/in/corpus.xlsx]
                    [--max-sheets N] [--max-rows N] [--max-cells N]
                    [--assets-dir ...] [--assets-rel ...] [--no-extract-images]

    The asset flags are accepted for a uniform extractor surface and ignored:
    cell values are text, embedded pictures and charts are not extracted.

Effect:
    Writes <output_md> with YAML frontmatter + Markdown body. Stdout receives
    a single JSON line summarizing the result.

Safety (the workbook is untrusted input):
    1. Before openpyxl touches the file, a stdlib zipfile pre-check reads the
       central directory only: member count, per-member and total
       uncompressed size, per-member compression ratio, encrypted members.
       A zip bomb is refused ({"ok": false, "refused": true, "reason": ...})
       without a byte being inflated. zipfile never yields more than a
       member's declared size, so the declared sizes bound what openpyxl can
       read later.
    2. openpyxl parses only through defusedxml: OPENPYXL_LXML=False routes
       every parse (fromstring and iterparse) through ElementTree, and
       defusedxml replaces both (entity declarations and external references
       raise). The extractor verifies that openpyxl picked defusedxml up and
       refuses to run otherwise — it never parses unprotected.
    3. load_workbook(read_only=True, data_only=True, keep_links=False):
       worksheets stream, formula cells give the value Excel cached, external
       links are not read. Each sheet's stored <dimension> is dropped
       (reset_dimensions) so a bogus A1:XFD1048576 cannot pad every row.

Caps (a large workbook is truncated, never silently):
    --max-sheets (default 100), --max-rows per sheet (10000 non-empty rows)
    and --max-cells (300000 emitted cells, row padding included) bound what
    is written; rows scanned (1000000, empty ones included), cells scanned
    (5000000) and a 120 s wall-clock budget bound the work. Past any cap the
    body gets an explicit `> **[truncated]** ...` line where the cut is, and
    the JSON result carries "truncated": true and "truncation": [reasons].
    Truncation rather than refusal because a big but legitimate workbook
    still has a useful head; the marker and the flag make the cut visible to
    the reader and to the caller.

Body:
    `## <sheet title>` per sheet (`(hidden)` marked), then a pipe table whose
    first column is the Excel row number and whose header is the column
    letters of the sheet's used range, so a cell can be cited as B7. Dates
    are ISO (2026-09-25, 2026-09-25 14:30), times HH:MM[:SS], booleans
    TRUE/FALSE, integral floats without ".0". `|` is escaped, line breaks in
    a cell become <br>. Empty rows are left out; an empty sheet says so.
"""
from __future__ import annotations

import os

# Before anything imports openpyxl: every XML parse through ElementTree, which
# defusedxml replaces. Checked again after the import (_openpyxl()).
os.environ["OPENPYXL_LXML"] = "False"
os.environ["OPENPYXL_DEFUSEDXML"] = "True"

import argparse  # noqa: E402
import datetime  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import zipfile  # noqa: E402
from pathlib import Path  # noqa: E402

from _common import (  # noqa: E402
    add_assets_args,
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


EXTRACTOR_NAME = "openpyxl"

# Zip pre-check (central directory only).
MAX_MEMBERS = 10_000
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_RATIO = 200                      # uncompressed / compressed, per member ...
RATIO_MIN_BYTES = 1024 * 1024        # ... once it unpacks past 1 MiB

# What is written.
DEFAULT_MAX_SHEETS = 100
DEFAULT_MAX_ROWS = 10_000            # non-empty rows per sheet
DEFAULT_MAX_CELLS = 300_000          # emitted cells across the workbook
# What is worked through.
MAX_SCAN_ROWS = 1_000_000
MAX_SCAN_CELLS = 5_000_000
TIME_BUDGET_S = 120
CELL_CHARS = 2000


class Refused(Exception):
    """The workbook is not read at all: a zip bomb, an encrypted member."""


class Unprotected(Exception):
    """openpyxl did not pick defusedxml up: nothing is parsed."""


def _human(n: int) -> str:
    return f"{n / 1048576:.1f} MiB" if n >= 1048576 else f"{n} B"


def zip_precheck(path: Path) -> None:
    """Raise Refused when the archive's central directory describes a zip bomb
    or something openpyxl should not open. Inflates nothing."""
    if not zipfile.is_zipfile(path):
        raise Refused("not a zip archive (an .xlsx is one)")
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise Refused(f"{len(infos)} zip members, over the {MAX_MEMBERS} cap")
    total = 0
    for info in infos:
        if info.flag_bits & 0x1:
            raise Refused(f"{info.filename} is encrypted")
        if info.file_size > MAX_MEMBER_BYTES:
            raise Refused(f"{info.filename} unpacks to {_human(info.file_size)}, "
                          f"over the {_human(MAX_MEMBER_BYTES)} per-member cap")
        if info.file_size > RATIO_MIN_BYTES and info.file_size > MAX_RATIO * max(info.compress_size, 1):
            ratio = info.file_size // max(info.compress_size, 1)
            raise Refused(f"{info.filename} unpacks {ratio}x ({_human(info.compress_size)} → "
                          f"{_human(info.file_size)}), over the {MAX_RATIO}x ratio cap")
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise Refused(f"the archive unpacks past the {_human(MAX_TOTAL_BYTES)} total cap")


def _openpyxl():
    """openpyxl, once it is certain every parse goes through defusedxml."""
    try:
        import defusedxml.ElementTree  # type: ignore  # noqa: F401
    except ImportError as e:
        raise Unprotected(f"defusedxml is not installed ({e}); run ensure_env.py to update the venv") from e
    import openpyxl  # type: ignore
    from openpyxl import xml as oxml  # type: ignore
    from openpyxl.xml import functions as oxf  # type: ignore
    routed = (getattr(oxf.fromstring, "__module__", ""), getattr(oxf.iterparse, "__module__", ""))
    if not oxml.DEFUSEDXML or oxml.LXML or not all(m.startswith("defusedxml") for m in routed):
        raise Unprotected(f"openpyxl is not parsing through defusedxml (DEFUSEDXML={oxml.DEFUSEDXML}, "
                          f"LXML={oxml.LXML}, parsers from {routed})")
    return openpyxl


def _root(e: BaseException) -> BaseException:
    """The first exception of a chain: openpyxl wraps parser errors in a
    generic ValueError("Unable to read workbook ...")."""
    seen = set()
    while id(e) not in seen and (e.__cause__ or e.__context__):
        seen.add(id(e))
        e = e.__cause__ or e.__context__
    return e


def _defused(e: BaseException) -> bool:
    """True when defusedxml stopped the parse (a DTD entity, an external
    reference): hostile XML, refused rather than failed."""
    try:
        from defusedxml import DefusedXmlException  # type: ignore
    except ImportError:
        return False
    return isinstance(_root(e), DefusedXmlException)


def _column_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def format_value(v) -> str:
    """One cell as Markdown table text."""
    if v is None:
        return ""
    if isinstance(v, bool):
        s = "TRUE" if v else "FALSE"
    elif isinstance(v, datetime.datetime):
        if (v.hour, v.minute, v.second, v.microsecond) == (0, 0, 0, 0):
            s = v.date().isoformat()
        else:
            s = v.isoformat(sep=" ", timespec="seconds" if v.second or v.microsecond else "minutes")
    elif isinstance(v, datetime.date):
        s = v.isoformat()
    elif isinstance(v, datetime.time):
        s = v.isoformat(timespec="seconds" if v.second or v.microsecond else "minutes")
    elif isinstance(v, datetime.timedelta):
        s = str(v)
    elif isinstance(v, float):
        s = str(int(v)) if v.is_integer() and abs(v) < 1e15 else format(v, ".15g")
    else:
        s = str(v)
    s = s.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(s) > CELL_CHARS:
        s = s[:CELL_CHARS] + "…"
    return s.replace("|", "\\|").replace("\n", "<br>")


def extract(path: Path, max_sheets: int, max_rows: int, max_cells: int,
            budget_s: float = TIME_BUDGET_S) -> tuple[str, dict, list[str]]:
    """(body, extras, warnings). Raises Refused / Unprotected before parsing."""
    zip_precheck(path)
    openpyxl = _openpyxl()
    deadline = time.monotonic() + budget_s
    warnings: list[str] = []
    truncation: list[str] = []
    out: list[str] = []
    titles: list[str] = []
    scanned_rows = scanned_cells = emitted_rows = emitted_cells = 0
    stop = False

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True, keep_links=False)
    try:
        sheets = wb.worksheets
        if len(sheets) > max_sheets:
            truncation.append(f"{len(sheets)} sheets, only the first {max_sheets} are extracted")
        for ws in sheets[:max_sheets]:
            title = sanitize_heading(str(ws.title)) or "(untitled)"
            titles.append(title)
            hidden = getattr(ws, "sheet_state", "visible") != "visible"
            out += [f"## {title}{' (hidden)' if hidden else ''}", ""]
            if hasattr(ws, "reset_dimensions"):
                ws.reset_dimensions()
            rows: list[tuple[int, dict[int, str]]] = []
            cut = None
            for rownum, row in enumerate(ws.iter_rows(values_only=True), start=1):
                scanned_rows += 1
                scanned_cells += len(row)
                if scanned_rows > MAX_SCAN_ROWS or scanned_cells > MAX_SCAN_CELLS:
                    cut = (f"the workbook's rows are scanned only up to {MAX_SCAN_ROWS} rows / "
                           f"{MAX_SCAN_CELLS} cells; the rest is not extracted")
                    stop = True
                    break
                if not scanned_rows & 1023 and time.monotonic() > deadline:
                    cut = f"not read within {budget_s:g} s; the rest is not extracted"
                    stop = True
                    break
                cells = {i: t for i, v in enumerate(row, start=1) if v is not None and (t := format_value(v))}
                if not cells:
                    continue
                if len(rows) >= max_rows:
                    cut = f"sheet \"{title}\" has more than {max_rows} non-empty rows; the rest is not extracted"
                    break
                width = max(cells) - min(cells) + 1
                if emitted_cells + width > max_cells:
                    cut = f"more than {max_cells} cells in the workbook; the rest is not extracted"
                    stop = True
                    break
                emitted_cells += width
                rows.append((rownum, cells))
            if rows:
                first = min(min(c) for _, c in rows)
                last = max(max(c) for _, c in rows)
                cols = range(first, last + 1)
                out.append("| row | " + " | ".join(_column_letter(c) for c in cols) + " |")
                out.append("|---:|" + "|".join("---" for _ in cols) + "|")
                for rownum, cells in rows:
                    out.append(f"| {rownum} | " + " | ".join(cells.get(c, "") for c in cols) + " |")
                emitted_rows += len(rows)
            elif not cut:
                out.append("_(empty sheet)_")
            if cut:
                truncation.append(cut)
                out += ["", f"> **[truncated]** {cut}"]
            out.append("")
            if stop:
                if len(titles) < min(len(sheets), max_sheets):
                    truncation.append(f"{min(len(sheets), max_sheets) - len(titles)} more sheet(s) not extracted")
                break
        if len(sheets) > max_sheets and not stop:
            out += [f"> **[truncated]** {len(sheets)} sheets, only the first {max_sheets} are extracted", ""]
        sheet_count = len(sheets)
    finally:
        wb.close()

    warnings += [f"truncated: {t}" for t in truncation]
    body = "\n".join(out).rstrip() + "\n"
    extras = {
        "sheets": sheet_count,
        "sheets_extracted": len(titles),
        "rows": emitted_rows,
        "cells": emitted_cells,
        "truncated": bool(truncation),
        "truncation": truncation,
        "headings": titles,
    }
    return body, extras, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract XLSX → Markdown via openpyxl (read-only, defusedxml).")
    ap.add_argument("input_xlsx")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    ap.add_argument("--max-sheets", type=int, default=DEFAULT_MAX_SHEETS)
    ap.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS, help="non-empty rows per sheet")
    ap.add_argument("--max-cells", type=int, default=DEFAULT_MAX_CELLS, help="emitted cells across the workbook")
    add_assets_args(ap)  # accepted for a uniform surface; xlsx images are not extracted
    args = ap.parse_args()

    in_path = Path(args.input_xlsx).expanduser().resolve()
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
        body, extras, warnings = extract(in_path, max(1, args.max_sheets), max(1, args.max_rows), max(1, args.max_cells))
    except Refused as e:
        emit_failure(f"refused: {e}", extra={"refused": True, "input": str(in_path)})
        return 1
    except Unprotected as e:
        emit_failure(f"refusing to parse without defusedxml: {e}", extra={"input": str(in_path)})
        return 2
    except Exception as e:
        root = _root(e)
        why = f"{type(root).__name__}: {str(root)[:200]}"
        if _defused(e):
            emit_failure(f"refused: the workbook's XML declares entities or external references ({why})",
                         extra={"refused": True, "input": str(in_path)})
            return 1
        if root is not e:
            why = f"{type(e).__name__}: {str(e)[:120]} ({why})"
        emit_failure(f"extraction failed: {why}", extra={"input": str(in_path)})
        return 1

    fm = {
        "id": args.doc_id,
        "source": source_rel,
        "source_type": "xlsx",
        "source_sha256": sha256_of(in_path),
        "extraction_method": f"{EXTRACTOR_NAME}@{tool_version_string()}",
        "extraction_date": today_iso(),
        "sheets": extras["sheets"],
        "rows": extras["rows"],
        "truncated": extras["truncated"],
        "headings": extras["headings"],
        "tokens_estimated": count_tokens(body),
        "warnings": warnings,
    }
    write_md(out_path, fm, body)
    emit_success(out_path, body, warnings, extra={k: extras[k] for k in (
        "sheets", "sheets_extracted", "rows", "cells", "truncated", "truncation")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
