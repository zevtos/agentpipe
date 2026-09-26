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
    1. Before openpyxl touches the file, a stdlib zipfile pre-check:
       - the central directory: member count, per-member and total
         uncompressed size (sharedStrings.xml lower still: openpyxl loads it
         whole into memory), per-member compression ratio, encrypted members;
       - then every XML part (.xml/.rels/.vml) is streamed once, in C-speed
         bytes scans: a part that is not UTF-8, any `<!DOCTYPE`/`<!ENTITY`
         (so a DTD — internal or external — is refused, never merely left
         unloaded), a run of more than 256 KiB between two `<` (past Excel's
         32767-character cell and 8192-character formula limits), and in a
         worksheet a row with more than 16384 cells (Excel's column limit:
         openpyxl materializes a whole row at once, so a longer one is refused
         before it can be).
       Any of these refuses the file ({"ok": false, "refused": true,
       "reason": ...}). zipfile never yields more than a member's declared
       size, so the declared sizes bound what openpyxl can read later.
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
    --max-sheets (default 100), --max-rows per sheet (10000 non-empty rows),
    --max-cells (300000 cells written, table padding included) and an 8 MiB
    output backstop bound what is written; rows scanned (1000000, empty ones
    included), cells scanned (5000000) and a 120 s wall-clock budget (checked
    per row, while rendering and before the token count) bound the work. A
    cell longer than 2000 characters is cut with "…". Every cut puts an
    explicit `> **[truncated]** ...` line in the body, and the JSON result
    carries "truncated": true and "truncation": ["<kind>: <reason>", ...]
    with kind one of sheets, rows, cells, scan, time, output, cell_chars.
    Truncation rather than refusal because a big but legitimate workbook
    still has a useful head; the marker and the flag make the cut visible to
    the reader and to the caller.

Body:
    `## <sheet title>` per sheet (`(hidden)` marked). A sheet whose used
    columns span at most 64 and whose padded table fits the cell cap is a
    pipe table: the Excel row number, then the column letters of the used
    range, so a cell can be cited as B7. A wider sheet is rendered sparse —
    `row 7: A=…; XFD=…`, the non-empty cells only — so a stray cell at XFD
    costs one cell, not 16384 per row, and no value is dropped. Dates are ISO
    (2026-09-25, 2026-09-25 14:30), times HH:MM[:SS], booleans TRUE/FALSE,
    integral floats without ".0". `|` is escaped, line breaks in a cell
    become <br>. Empty rows are left out; an empty sheet says so.
"""
from __future__ import annotations

import os

# Before anything imports openpyxl: every XML parse through ElementTree, which
# defusedxml replaces. Checked again after the import (_openpyxl()).
os.environ["OPENPYXL_LXML"] = "False"
os.environ["OPENPYXL_DEFUSEDXML"] = "True"

import argparse  # noqa: E402
import datetime  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import zipfile  # noqa: E402
from pathlib import Path  # noqa: E402

from _common import (  # noqa: E402
    add_assets_args,
    count_tokens,
    emit_failure,
    json_stdout,
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
SST_MAX_BYTES = 32 * 1024 * 1024     # sharedStrings.xml: openpyxl holds every string in memory
# The streamed scan of each XML part.
XML_SUFFIXES = (".xml", ".rels", ".vml")
MAX_ROW_CELLS = 16384                # Excel's column limit
MAX_TEXT_RUN = 256 * 1024            # bytes between two '<'
SCAN_CHUNK = 1 << 20
_DTD_RE = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_ROW_CELL_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?(row|c)[\s/>]")

# What is written.
DEFAULT_MAX_SHEETS = 100
DEFAULT_MAX_ROWS = 10_000            # non-empty rows per sheet
DEFAULT_MAX_CELLS = 300_000          # emitted cells across the workbook
# What is worked through.
MAX_SCAN_ROWS = 1_000_000
MAX_SCAN_CELLS = 5_000_000
TIME_BUDGET_S = 120
CELL_CHARS = 2000
MAX_SPAN = 64                        # columns a dense table may span; wider renders sparse
MAX_OUTPUT_BYTES = 8 * 1024 * 1024   # body backstop
EXACT_TOKENS_CHARS = 2_000_000       # past it the token count is an estimate (bytes / 4)


class Refused(Exception):
    """The workbook is not read at all: a zip bomb, an encrypted member."""


class Unprotected(Exception):
    """openpyxl did not pick defusedxml up: nothing is parsed."""


def _human(n: int) -> str:
    return f"{n / 1048576:.1f} MiB" if n >= 1048576 else f"{n} B"


def zip_precheck(path: Path, deadline: float | None = None) -> None:
    """Raise Refused when the archive describes a zip bomb or carries XML openpyxl
    should not see. The central directory first (nothing inflated), then one
    streamed bytes scan of each XML part (_scan_xml)."""
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
            if info.filename.rsplit("/", 1)[-1].lower() == "sharedstrings.xml" and info.file_size > SST_MAX_BYTES:
                raise Refused(f"{info.filename} unpacks to {_human(info.file_size)}, over the "
                              f"{_human(SST_MAX_BYTES)} shared-strings cap (openpyxl holds them all in memory)")
            if info.file_size > RATIO_MIN_BYTES and info.file_size > MAX_RATIO * max(info.compress_size, 1):
                ratio = info.file_size // max(info.compress_size, 1)
                raise Refused(f"{info.filename} unpacks {ratio}x ({_human(info.compress_size)} → "
                              f"{_human(info.file_size)}), over the {MAX_RATIO}x ratio cap")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise Refused(f"the archive unpacks past the {_human(MAX_TOTAL_BYTES)} total cap")
        for info in infos:
            if not info.is_dir() and info.filename.lower().endswith(XML_SUFFIXES):
                _scan_xml(zf, info, "/worksheets/" in info.filename, deadline)


def _scan_xml(zf: zipfile.ZipFile, info: zipfile.ZipInfo, sheet: bool, deadline: float | None) -> None:
    """One XML part, streamed in SCAN_CHUNK pieces with C-speed bytes scans only:
    refuse a part that is not UTF-8, any <!DOCTYPE / <!ENTITY, a run of more than
    MAX_TEXT_RUN bytes between two '<', and (worksheet) a row of more than
    MAX_ROW_CELLS <c> elements. Linear in the part's size."""
    name = info.filename
    run = cells = 0
    carry = b""
    first = True
    with zf.open(info) as fh:
        while True:
            data = fh.read(SCAN_CHUNK)
            if first:
                first = False
                if data[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in data[:4]:
                    raise Refused(f"{name} is not UTF-8 XML")
            if deadline is not None and time.monotonic() > deadline:
                raise Refused(f"{name}: the pre-check did not finish within {TIME_BUDGET_S} s")
            if data:
                pieces = data.split(b"<")
                run += len(pieces[0])
                longest = max(map(len, pieces[1:-1]), default=0)
                if run > MAX_TEXT_RUN or longest > MAX_TEXT_RUN:
                    raise Refused(f"{name}: more than {MAX_TEXT_RUN // 1024} KiB between two tags "
                                  "(past Excel's own cell and formula limits)")
                if len(pieces) > 1:
                    run = len(pieces[-1])
            buf = carry + data
            if _DTD_RE.search(buf):
                raise Refused(f"{name} declares a DTD (<!DOCTYPE/<!ENTITY)")
            end = len(buf) if not data else max(0, len(buf) - 16)
            pos = 0
            if sheet:
                for m in _ROW_CELL_RE.finditer(buf):
                    if m.start() >= end:
                        break
                    if m.group(1) == b"row":
                        cells = 0
                    else:
                        cells += 1
                        if cells > MAX_ROW_CELLS:
                            raise Refused(f"{name}: a row with more than {MAX_ROW_CELLS} cells "
                                          "(past Excel's column limit)")
                    pos = m.end()
            carry = buf[max(pos, end):]
            if not data:
                return


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


def format_value(v) -> tuple[str, bool]:
    """One cell as Markdown table text, and whether CELL_CHARS cut it."""
    if v is None:
        return "", False
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
    cut = len(s) > CELL_CHARS
    if cut:
        s = s[:CELL_CHARS] + "…"
    return s.replace("|", "\\|").replace("\n", "<br>"), cut


class _Body:
    """The body's lines, bounded by max_bytes; marker lines always fit (a small reserve)."""

    RESERVE = 4096

    def __init__(self, max_bytes: int):
        self.lines: list[str] = []
        self.size, self.max = 0, max_bytes

    def add(self, line: str, marker: bool = False) -> bool:
        n = len(line.encode("utf-8")) + 1
        if not marker and self.size + n > self.max - self.RESERVE:
            return False
        self.lines.append(line)
        self.size += n
        return True


def extract(path: Path, max_sheets: int, max_rows: int, max_cells: int,
            budget_s: float = TIME_BUDGET_S, max_span: int = MAX_SPAN,
            max_output: int = MAX_OUTPUT_BYTES) -> tuple[str, dict, list[str]]:
    """(body, extras, warnings). Raises Refused / Unprotected before parsing."""
    deadline = time.monotonic() + budget_s
    zip_precheck(path, deadline)
    openpyxl = _openpyxl()
    body = _Body(max_output)
    truncation: list[str] = []
    titles: list[str] = []
    scanned_rows = scanned_cells = emitted_rows = rendered = 0
    cut_cells_total = 0

    def late() -> bool:
        return time.monotonic() > deadline

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True, keep_links=False)
    try:
        sheets = wb.worksheets
        sheet_count = len(sheets)
        stop = None  # (kind, reason) once a workbook-wide cap is hit
        for ws in sheets[:max_sheets]:
            title = sanitize_heading(str(ws.title)) or "(untitled)"
            titles.append(title)
            hidden = getattr(ws, "sheet_state", "visible") != "visible"
            body.add(f"## {title}{' (hidden)' if hidden else ''}", marker=True)
            body.add("", marker=True)
            if hasattr(ws, "reset_dimensions"):
                ws.reset_dimensions()
            rows: list[tuple[int, dict[int, str]]] = []
            nonempty = cut_cells = 0
            sheet_cut = None
            for rownum, row in enumerate(ws.iter_rows(values_only=True), start=1):
                scanned_rows += 1
                scanned_cells += len(row)
                if scanned_rows > MAX_SCAN_ROWS or scanned_cells > MAX_SCAN_CELLS:
                    stop = ("scan", f"rows are scanned only up to {MAX_SCAN_ROWS} rows / {MAX_SCAN_CELLS} cells "
                                    "per workbook; the rest is not extracted")
                    break
                if late():
                    stop = ("time", f"not read within {budget_s:g} s; the rest is not extracted")
                    break
                cells: dict[int, str] = {}
                row_cut = 0
                for i, v in enumerate(row, start=1):
                    if v is None:
                        continue
                    text, was_cut = format_value(v)
                    if text:
                        cells[i] = text
                        row_cut += was_cut
                if not cells:
                    continue
                if len(rows) >= max_rows:
                    sheet_cut = ("rows", f"sheet \"{title}\" has more than {max_rows} non-empty rows; "
                                         "the rest is not extracted")
                    break
                if rendered + nonempty + len(cells) > max_cells:
                    stop = ("cells", f"more than {max_cells} cells in the workbook; the rest is not extracted")
                    break
                nonempty += len(cells)
                cut_cells += row_cut
                rows.append((rownum, cells))
            cut = stop or sheet_cut
            wrote_cells, wrote_rows, render_cut = _render(body, rows, nonempty, max_cells - rendered, max_span,
                                                          late, budget_s)
            rendered += wrote_cells
            emitted_rows += wrote_rows
            if render_cut:
                stop = cut = render_cut
            if not rows and not cut:
                body.add("_(empty sheet)_", marker=True)
            if cut_cells:
                cut_cells_total += cut_cells
                body.add("", marker=True)
                body.add(f"> **[truncated]** {cut_cells} cell(s) of this sheet are longer than {CELL_CHARS} "
                         "characters and end in «…»", marker=True)
            if cut:
                truncation.append(f"{cut[0]}: {cut[1]}")
                body.add("", marker=True)
                body.add(f"> **[truncated]** {cut[1]}", marker=True)
            body.add("", marker=True)
            if stop:
                left = min(sheet_count, max_sheets) - len(titles)
                if left:
                    truncation.append(f"{stop[0]}: {left} more sheet(s) not extracted")
                break
        if sheet_count > max_sheets and not stop:
            truncation.append(f"sheets: {sheet_count} sheets, only the first {max_sheets} are extracted")
            body.add(f"> **[truncated]** {sheet_count} sheets, only the first {max_sheets} are extracted", marker=True)
            body.add("", marker=True)
    finally:
        wb.close()

    if cut_cells_total:
        truncation.append(f"cell_chars: {cut_cells_total} cell(s) longer than {CELL_CHARS} characters cut, "
                          "each ending in «…»")
    warnings = [f"truncated: {t}" for t in truncation]
    text = "\n".join(body.lines).rstrip() + "\n"
    extras = {
        "sheets": sheet_count,
        "sheets_extracted": len(titles),
        "rows": emitted_rows,
        "cells": rendered,
        "cut_cells": cut_cells_total,
        "truncated": bool(truncation),
        "truncation": truncation,
        "headings": titles,
        "output_bytes": len(text.encode("utf-8")),
    }
    return text, extras, warnings


def _render(body: _Body, rows: list[tuple[int, dict[int, str]]], nonempty: int, budget: int,
            max_span: int, late, budget_s: float) -> tuple[int, int, tuple[str, str] | None]:
    """One sheet's rows into body: a dense pipe table when the used columns span at most
    max_span and the padded table fits budget cells, else sparse `row N: A=…; B=…` lines
    (nonempty ≤ budget, checked while scanning). Returns (cells written, padding
    included; rows written; the (kind, reason) that stopped it — the output cap or the
    deadline — or None)."""
    if not rows:
        return 0, 0, None
    first = min(min(c) for _, c in rows)
    last = max(max(c) for _, c in rows)
    span = last - first + 1
    dense = span <= max_span and len(rows) * span <= budget

    full = ("output", f"the body reached the {MAX_OUTPUT_BYTES // 1048576} MiB output cap; the rest is not extracted")
    cols = range(first, last + 1)
    head = (["| row | " + " | ".join(_column_letter(c) for c in cols) + " |", "|---:|" + "|".join("---" for _ in cols) + "|"]
            if dense else [f"_(sparse: the used columns span {_column_letter(first)}–{_column_letter(last)}, {span} wide; "
                           "each row lists its non-empty cells)_", ""])
    for line in head:
        if not body.add(line):
            return 0, 0, full
    cells_written = 0
    for k, (rownum, cells) in enumerate(rows):
        if not k & 255 and late():
            return cells_written, k, ("time", f"not rendered within {budget_s:g} s; the rest is not extracted")
        if dense:
            line = f"| {rownum} | " + " | ".join(cells.get(c, "") for c in cols) + " |"
        else:
            line = f"row {rownum}: " + "; ".join(f"{_column_letter(c)}={t}" for c, t in sorted(cells.items()))
        if not body.add(line):
            return cells_written, k, full
        cells_written += span if dense else len(cells)
    return cells_written, len(rows), None


def estimate_tokens(text: str, late) -> tuple[int, bool]:
    """(tokens, exact): tiktoken while the text is small enough and time is left,
    else bytes / 4 (≈ 4 chars/token Latin, ≈ 2 Cyrillic)."""
    if len(text) <= EXACT_TOKENS_CHARS and not late():
        return count_tokens(text), True
    return len(text.encode("utf-8")) // 4, False


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract XLSX → Markdown via openpyxl (read-only, defusedxml).")
    ap.add_argument("input_xlsx")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    ap.add_argument("--max-sheets", type=int, default=DEFAULT_MAX_SHEETS)
    ap.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS, help="non-empty rows per sheet")
    ap.add_argument("--max-cells", type=int, default=DEFAULT_MAX_CELLS,
                    help="cells written across the workbook, table padding included")
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

    started = time.monotonic()
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

    tokens, exact = estimate_tokens(body, lambda: time.monotonic() > started + TIME_BUDGET_S)
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
        "tokens_estimated": tokens,
        "warnings": warnings,
    }
    write_md(out_path, fm, body)
    payload = {"ok": True, "out": str(out_path), "tokens_estimated": tokens, "tokens_exact": exact,
               "warnings": warnings}
    payload.update({k: extras[k] for k in ("sheets", "sheets_extracted", "rows", "cells", "cut_cells",
                                           "output_bytes", "truncated", "truncation")})
    json_stdout(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
