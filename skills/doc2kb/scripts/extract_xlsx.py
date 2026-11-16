#!/usr/bin/env python3
"""
extract_xlsx.py — Phase 4 extractor for Excel workbooks (.xlsx / .xlsm).

CLI:
    extract_xlsx.py <input_xlsx> <output_md>
                    [--doc-id doc-NNN]
                    [--source-rel relative/path/in/corpus.xlsx]
                    [--max-sheets N] [--max-rows N] [--max-cells N]
                    [--timeout S] [--max-rss-mb N]
                    [--assets-dir ...] [--assets-rel ...] [--no-extract-images]

    The asset flags are accepted for a uniform extractor surface and ignored:
    cell values are text, embedded pictures and charts are not extracted.

Effect:
    Writes <output_md> with YAML frontmatter + Markdown body. Stdout receives
    a single JSON line summarizing the result.

Safety (the workbook is untrusted input):
    0. The OS bounds the work, whatever path openpyxl takes: everything below runs
       in a child process of this script. The parent polls the child's RSS every
       20 ms (/proc on Linux, libproc's proc_pid_rusage on macOS; ps every
       100 ms elsewhere) and SIGKILLs it past the wall deadline
       (--timeout / DOC2KB_XLSX_TIMEOUT, default 20 s) or the memory ceiling
       (--max-rss-mb / DOC2KB_XLSX_MAX_RSS_MB, default 1024 MiB); the child also
       runs under RLIMIT_CPU (the deadline + 2 s) and bounds itself: a watchdog
       thread reads a pipe whose write end only the parent holds (EOF: the parent
       died, by any signal → the child removes the temp dir and exits at once;
       getppid is the backup check), and enforces the same RSS ceiling and the
       wall deadline + 1 s on its own. SIGTERM/SIGHUP to the parent kill the
       child and remove the temp dir. The temp dir (0700, doc2kb-xlsx-<pid>-*)
       of a run whose parent and child both died is swept by the next run once
       older than 2 minutes. Where RSS cannot be read (Windows), only the wall
       deadline applies and the result says so in its warnings. A kill is
       {"ok": false, "refused": true, "reason": "refused:
       resource: time|memory (...)"}; the result carries "peak_rss_mib" for
       parent and child. The child's own deadline is 75 % of the wall deadline,
       so a workbook that is merely large ends truncated with a marker first.
       The checks below are defence in depth under that bound.
    1. Before openpyxl touches the file, a stdlib zipfile pre-check:
       - the central directory: member count, per-member and total
         uncompressed size (sharedStrings.xml lower still: openpyxl loads it
         whole into memory), per-member compression ratio, encrypted members;
       - then EVERY member is streamed once, whatever its name (openpyxl finds
         parts by relationship and content type), in C-speed bytes scans: any
         `<!DOCTYPE`/`<!ENTITY` (so a DTD — internal or external — is refused,
         never merely left unloaded). A member that is XML by name or by its
         first bytes is also refused when it is not UTF-8, when its declaration
         names an encoding that would hide markup from the scan (UTF-16,
         EBCDIC, unknown), on a run of more than 256 KiB between two `<` (past
         Excel's 32767-character cell and 8192-character formula limits), and
         on a row with more than 16384 cells (Excel's column limit).
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
       links are not read. Rows come straight from openpyxl's own worksheet
       parser (the one iter_rows wraps) — no gap filling, so neither a bogus
       <dimension> nor a row-number jump pads anything, and a row numbered at
       or below an earlier one (which iter_rows silently drops) is kept, in file
       order, with a `row_order` warning.

Caps (a large workbook is truncated, never silently):
    --max-sheets (default 100), --max-rows per sheet (10000 non-empty rows),
    --max-cells (300000 cells written, table padding included) and an 8 MiB
    output cap bound what is written — the cap applies as rows arrive (a row
    is held only while the text held so far fits it), so no sheet's cells are
    held past it; rows scanned (1000000), cells scanned (5000000) and the
    child's own deadline (checked per row, while rendering and before the
    token count) bound the work. A
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
import codecs  # noqa: E402
import datetime  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import zipfile  # noqa: E402
from pathlib import Path  # noqa: E402

from _common import (  # noqa: E402
    add_assets_args,
    count_tokens,
    emit_failure,
    json_stdout,
    resolve_assets_dir,
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
# The streamed scan of every member (DTD markers in all of them; the XML rules in the
# members that are XML by name or by content).
XML_SUFFIXES = (".xml", ".rels", ".vml")
MAX_ROW_CELLS = 16384                # Excel's column limit
MAX_TEXT_RUN = 256 * 1024            # bytes between two '<'
SCAN_CHUNK = 1 << 20
_DTD_RE = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_ROW_CELL_RE = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?(row|c)[\s/>]")
_XML_DECL_ENC_RE = re.compile(rb"<\?xml[^>]{0,200}?encoding\s*=\s*[\"']([A-Za-z0-9._:-]{1,40})[\"']")
_MARKUP_PROBE = "<!DOCTYPE<!doctype<!ENTITY<!entity"

# What is written.
DEFAULT_MAX_SHEETS = 100
DEFAULT_MAX_ROWS = 10_000            # non-empty rows per sheet
DEFAULT_MAX_CELLS = 300_000          # emitted cells across the workbook
# What is worked through.
MAX_SCAN_ROWS = 1_000_000
MAX_SCAN_CELLS = 5_000_000
CELL_CHARS = 2000
MAX_SPAN = 64                        # columns a dense table may span; wider renders sparse
MAX_OUTPUT_BYTES = 8 * 1024 * 1024   # body backstop
EXACT_TOKENS_CHARS = 2_000_000       # past it the token count is an estimate (bytes / 4)

# The OS bound: the openpyxl work runs in a child process the parent kills past either
# (supervise()). Overridable by env or CLI (the CLI wins).
DEFAULT_TIMEOUT_S = 20.0             # wall seconds: DOC2KB_XLSX_TIMEOUT / --timeout
DEFAULT_MAX_RSS_MB = 1024            # child RSS ceiling: DOC2KB_XLSX_MAX_RSS_MB / --max-rss-mb
POLL_S = 0.02                        # how often the parent reads the child's RSS (/proc, libproc) ...
POLL_PS_S = 0.1                      # ... or through ps, where neither exists
SOFT_SHARE = 0.75                    # the child's own deadline: truncate with a marker before the kill
CHILD_FLAG = "--_xlsx-child"
TEMP_PREFIX = "doc2kb-xlsx-"         # + the parent's pid: tempfile.gettempdir()/doc2kb-xlsx-<pid>-<random>
STALE_S = 120                        # a temp dir whose parent and child are both gone is swept past this age


class Refused(Exception):
    """The workbook is not read at all: a zip bomb, an encrypted member."""


class Unprotected(Exception):
    """openpyxl did not pick defusedxml up: nothing is parsed."""


def _human(n: int) -> str:
    return f"{n / 1048576:.1f} MiB" if n >= 1048576 else f"{n} B"


def zip_precheck(path: Path, deadline: float | None = None) -> None:
    """Raise Refused when the archive describes a zip bomb or carries XML openpyxl
    should not see. The central directory first (nothing inflated), then one
    streamed bytes scan of every member, whatever its name (_scan_member): openpyxl
    finds parts by relationship and content type, not by name."""
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
            if not info.is_dir():
                _scan_member(zf, info, deadline)


def _ascii_markup(encoding: str) -> bool:
    """True when the encoding writes <!DOCTYPE and <!ENTITY as their ASCII bytes, so the
    bytes scan sees them (UTF-8, cp1251, latin-1 …; not UTF-16, EBCDIC or unknown ones)."""
    try:
        name = codecs.lookup(encoding).name
        return _MARKUP_PROBE.encode(name) == _MARKUP_PROBE.encode("ascii")
    except (LookupError, UnicodeError, ValueError):
        return False


def _scan_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, deadline: float | None) -> None:
    """One member, streamed in SCAN_CHUNK pieces with C-speed bytes scans only.

    Every member: refuse any <!DOCTYPE / <!ENTITY. A member that is XML by name
    (.xml/.rels/.vml) or by content (its first bytes, past a UTF-8 BOM and blanks, are
    '<', or it looks like UTF-16): refuse it when it is not UTF-8 (a BOM or NUL in its
    first four bytes), when its XML declaration names an encoding that would hide
    markup from this scan, on a run of more than MAX_TEXT_RUN bytes between two '<',
    and on a row of more than MAX_ROW_CELLS <c> elements. Linear in the member's size."""
    name = info.filename
    run = 0
    cells = None  # counted from the first <row> on
    carry = b""
    first = True
    xmlish = False
    with zf.open(info) as fh:
        while True:
            data = fh.read(SCAN_CHUNK)
            if first:
                first = False
                utf16 = data[:2] in (b"\xff\xfe", b"\xfe\xff", b"<\x00", b"\x00<")
                head = data[:1024]
                head = head[3:] if head.startswith(b"\xef\xbb\xbf") else head
                head = head.lstrip()
                xmlish = name.lower().endswith(XML_SUFFIXES) or head.startswith(b"<") or utf16
                if xmlish and (utf16 or b"\x00" in data[:4]):
                    raise Refused(f"{name} is not UTF-8 XML")
                m = _XML_DECL_ENC_RE.match(head) if xmlish else None
                if m and not _ascii_markup(m.group(1).decode("ascii")):
                    raise Refused(f"{name} declares the encoding {m.group(1).decode('ascii')}, "
                                  "which would hide markup from the DTD scan")
            if deadline is not None and time.monotonic() > deadline:
                raise Refused(f"{name}: the pre-check did not finish in time")
            if xmlish and data:
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
            if xmlish:
                for m in _ROW_CELL_RE.finditer(buf):
                    if m.start() >= end:
                        break
                    if m.group(1) == b"row":
                        cells = 0
                    elif cells is not None:
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


def _sheet_rows(wb, ws):
    """(row number, [(column, value)]) for each <row> as the file has it: openpyxl's own
    worksheet parser, which ReadOnlyWorksheet.iter_rows wraps — without the wrapper's gap
    filling, which yields millions of empty rows for a jump and silently drops a row whose
    number is not above the previous one. Values are the same (the same parse_cell).
    Second item: whether row order can be checked (False on the iter_rows fallback, for
    an openpyxl whose internals moved)."""
    try:
        from openpyxl.worksheet._reader import WorkSheetParser  # type: ignore
        src = ws._get_source()
        parser = WorkSheetParser(src, ws._shared_strings, data_only=wb.data_only, epoch=wb.epoch,
                                 date_formats=wb._date_formats, timedelta_formats=wb._timedelta_formats)
    except (ImportError, AttributeError, TypeError):
        if hasattr(ws, "reset_dimensions"):
            ws.reset_dimensions()
        return ((i, list(enumerate(row, start=1))) for i, row in
                enumerate(ws.iter_rows(values_only=True), start=1)), False

    def rows():
        try:
            for idx, cells in parser.parse():
                yield idx, [(c["column"], c["value"]) for c in cells]
        finally:
            src.close()
    return rows(), True


def extract(path: Path, max_sheets: int, max_rows: int, max_cells: int,
            budget_s: float = 15.0, max_span: int = MAX_SPAN,
            max_output: int = MAX_OUTPUT_BYTES) -> tuple[str, dict, list[str]]:
    """(body, extras, warnings). Raises Refused / Unprotected before parsing. Runs in the
    child process (child_main); budget_s is its own deadline, short of the parent's kill."""
    deadline = time.monotonic() + budget_s
    zip_precheck(path, deadline)
    openpyxl = _openpyxl()
    body = _Body(max_output)
    truncation: list[str] = []
    titles: list[str] = []
    scanned_rows = scanned_cells = emitted_rows = rendered = 0
    cut_cells_total = 0
    row_order: list[str] = []

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
            source, ordered = _sheet_rows(wb, ws)
            rows: list[tuple[int, dict[int, str]]] = []
            nonempty = cut_cells = 0
            held = 0  # characters of the rows held, counted against the output cap as they arrive
            last = 0
            disorder, first_disorder = 0, None
            sheet_cut = None
            for rownum, row in source:
                scanned_rows += 1
                scanned_cells += len(row)
                if scanned_rows > MAX_SCAN_ROWS or scanned_cells > MAX_SCAN_CELLS:
                    stop = ("scan", f"rows are scanned only up to {MAX_SCAN_ROWS} rows / {MAX_SCAN_CELLS} cells "
                                    "per workbook; the rest is not extracted")
                    break
                if late():
                    stop = ("time", f"not read within {budget_s:g} s; the rest is not extracted")
                    break
                if ordered:
                    if rownum <= last:
                        disorder += 1
                        first_disorder = first_disorder or (rownum, last)
                    last = max(last, rownum)
                cells: dict[int, str] = {}
                row_cut = 0
                for i, v in row:
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
                weight = sum(len(t) for t in cells.values()) + 4 * len(cells) + 16
                if body.size + held + weight > max_output - _Body.RESERVE:
                    stop = ("output", f"the body reached the {max_output // 1048576} MiB output cap; "
                                      "the rest is not extracted")
                    break
                held += weight
                nonempty += len(cells)
                cut_cells += row_cut
                rows.append((rownum, cells))
            if disorder:
                row_order.append(f"sheet \"{title}\": {disorder} row(s) numbered at or below an earlier row "
                                 f"(row {first_disorder[0]} after row {first_disorder[1]}); kept, in file order")
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
    warnings = [f"truncated: {t}" for t in truncation] + [f"row_order: {r}" for r in row_order]
    text = "\n".join(body.lines).rstrip() + "\n"
    extras = {
        "sheets": sheet_count,
        "sheets_extracted": len(titles),
        "rows": emitted_rows,
        "cells": rendered,
        "cut_cells": cut_cells_total,
        "truncated": bool(truncation),
        "truncation": truncation,
        "row_order": row_order,
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


def _parser(child: bool) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Extract XLSX → Markdown via openpyxl (read-only, defusedxml), "
                                             "in a child process bounded by wall time and memory.")
    ap.add_argument("input_xlsx")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    ap.add_argument("--max-sheets", type=int, default=DEFAULT_MAX_SHEETS)
    ap.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS, help="non-empty rows per sheet")
    ap.add_argument("--max-cells", type=int, default=DEFAULT_MAX_CELLS,
                    help="cells written across the workbook, table padding included")
    ap.add_argument("--timeout", type=float, default=None,
                    help=f"wall seconds for the reader (env DOC2KB_XLSX_TIMEOUT, default {DEFAULT_TIMEOUT_S:g})")
    ap.add_argument("--max-rss-mb", type=int, default=None,
                    help=f"reader memory ceiling in MiB (env DOC2KB_XLSX_MAX_RSS_MB, default {DEFAULT_MAX_RSS_MB})")
    if child:
        ap.add_argument("--budget", type=float, required=True)
        ap.add_argument("--lifeline", type=int, default=None)
        ap.add_argument("--work", default=None)
    add_assets_args(ap)  # accepted for a uniform surface; xlsx images are not extracted
    return ap


def _limit(cli, env: str, default, kind):
    if cli is not None:
        return kind(cli)
    raw = os.environ.get(env)
    try:
        return kind(raw) if raw else default
    except ValueError:
        return default


_LIBPROC = None


def _libproc():
    """macOS's proc_pid_rusage through ctypes (stdlib): a process's resident size
    without spawning ps. False where it is not available."""
    global _LIBPROC
    if _LIBPROC is None:
        _LIBPROC = False
        if sys.platform == "darwin":
            try:
                import ctypes

                class RusageInfoV0(ctypes.Structure):
                    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
                        "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups",
                        "ri_pageins", "ri_wired_size", "ri_resident_size", "ri_phys_footprint",
                        "ri_proc_start_abstime", "ri_proc_exit_abstime")]

                lib = ctypes.CDLL("/usr/lib/libproc.dylib")
                lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
                lib.proc_pid_rusage.restype = ctypes.c_int
                _LIBPROC = (lib, RusageInfoV0, ctypes)
            except (OSError, AttributeError):
                _LIBPROC = False
    return _LIBPROC


def _cheap_rss() -> bool:
    return os.path.isdir("/proc/self") or bool(_libproc())


def _rss_kib(pid: int) -> int:
    """The process's resident set in KiB: /proc on Linux, libproc on macOS, else ps;
    0 when it is gone."""
    try:
        with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
        return 0
    except OSError:
        pass
    lp = _libproc()
    if lp:
        lib, info_t, ctypes = lp
        info = info_t()
        return info.ri_resident_size // 1024 if lib.proc_pid_rusage(pid, 0, ctypes.byref(info)) == 0 else 0
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout
        return int(out.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _maxrss_mib(who: int) -> float | None:
    """getrusage's peak RSS in MiB (bytes on macOS, KiB on Linux); None without the
    resource module (Windows)."""
    try:
        import resource
    except ImportError:
        return None
    v = resource.getrusage(who).ru_maxrss
    return round(v / 1048576 if sys.platform == "darwin" else v / 1024, 1)


def _alive(pid: int) -> bool:
    """True unless the process is certainly gone (POSIX signal 0; never on Windows,
    where os.kill would terminate it)."""
    if os.name == "nt" or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def sweep_stale(root: Path | None = None) -> int:
    """Remove temp dirs of earlier runs whose parent (the pid in the name) and child
    (child.pid inside) are both gone and that are older than STALE_S: what a parent
    killed with SIGKILL, together with its child, leaves behind. Returns the count."""
    root = Path(root or tempfile.gettempdir())
    swept = 0
    try:
        entries = list(root.glob(TEMP_PREFIX + "*"))
    except OSError:
        return 0
    now = time.time()
    for d in entries:
        m = re.match(re.escape(TEMP_PREFIX) + r"(\d+)-", d.name)
        try:
            if not m or not d.is_dir() or now - d.stat().st_mtime < STALE_S:
                continue
            child = int((d / "child.pid").read_text().strip() or 0) if (d / "child.pid").exists() else 0
        except (OSError, ValueError):
            continue
        if _alive(int(m.group(1))) or (child and _alive(child)):
            continue
        shutil.rmtree(d, ignore_errors=True)
        swept += 1
    return swept


class _Terminated(Exception):
    """SIGTERM / SIGHUP reached the parent: kill the child, clean up, exit."""


def _on_term(signum, frame):
    raise _Terminated(signum)


def supervise(argv: list[str], timeout_s: float, max_rss_mb: int, out, err,
              work: Path) -> tuple[int | None, str | None, float]:
    """Run argv, polling its RSS every POLL_S; SIGKILL it past timeout_s wall seconds or
    max_rss_mb MiB of RSS. (returncode, None, peak MiB seen) when it ends by itself,
    (None, "time" | "memory", peak) when it was killed.

    The child gets the read end of a pipe (--lifeline) whose write end only this
    process holds: when this process dies, by any signal, the child reads EOF and
    removes work and exits (child_watchdog). Its pid goes to work/child.pid for the
    sweep. The child is killed on every way out of here, a signal included."""
    interval = POLL_S if _cheap_rss() else POLL_PS_S
    lifeline = os.pipe() if os.name != "nt" else None
    if lifeline:
        argv = argv + ["--lifeline", str(lifeline[0])]
    try:
        proc = subprocess.Popen(argv, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                pass_fds=(lifeline[0],) if lifeline else ())
    finally:
        if lifeline:
            os.close(lifeline[0])
    try:
        (work / "child.pid").write_text(str(proc.pid))
    except OSError:
        pass
    deadline = time.monotonic() + timeout_s
    peak = 0
    try:
        while True:
            try:
                return proc.wait(timeout=interval), None, round(peak / 1024, 1)
            except subprocess.TimeoutExpired:
                pass
            rss = _rss_kib(proc.pid)
            peak = max(peak, rss)
            why = "memory" if rss > max_rss_mb * 1024 else "time" if time.monotonic() > deadline else None
            if why:
                proc.kill()
                proc.wait()
                return None, why, round(peak / 1024, 1)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if lifeline:
            os.close(lifeline[1])


def child_watchdog(lifeline: int | None, work: Path | None, max_rss_mb: int, hard_deadline: float) -> None:
    """The child bounds itself, so it stays bounded if the parent is gone:
    - a thread blocks on the lifeline pipe; EOF means the parent died (by any
      signal) → remove the temp dir, exit at once;
    - a thread checks every POLL_S that the parent is still the one that started it
      (getppid, the backup), that its own RSS is under max_rss_mb and that the wall
      deadline has not passed → a refusal line on stdout, exit."""
    ppid = os.getppid()

    def orphaned():
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
        os._exit(3)

    def refuse(kind: str, detail: str):
        line = json.dumps({"ok": False, "refused": True, "reason": f"refused: resource: {kind} ({detail})"})
        try:
            os.write(1, (line + "\n").encode("utf-8"))
        finally:
            os._exit(1)

    if lifeline is not None:
        def watch_pipe():
            try:
                while os.read(lifeline, 64):
                    pass
            except OSError:
                pass
            orphaned()
        threading.Thread(target=watch_pipe, name="xlsx-lifeline", daemon=True).start()

    me = os.getpid()
    readable = _rss_kib(me) > 0

    def watch_self():
        while True:
            time.sleep(POLL_S)
            if os.name != "nt" and os.getppid() != ppid:
                orphaned()
            if readable:
                rss = _rss_kib(me)
                if rss > max_rss_mb * 1024:
                    refuse("memory", f"the reader reached {rss / 1024:.1f} MiB, past the {max_rss_mb} MiB ceiling")
            if time.monotonic() > hard_deadline:
                refuse("time", "the reader ran past its deadline")
    threading.Thread(target=watch_self, name="xlsx-self", daemon=True).start()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == CHILD_FLAG:
        return child_main(sys.argv[2:])
    args = _parser(child=False).parse_args()
    in_path = Path(args.input_xlsx).expanduser().resolve()
    out_path = Path(args.output_md).expanduser().resolve()
    try:
        validate_source_rel(args.source_rel or in_path.name)
    except ValueError as e:
        emit_failure(f"invalid --source-rel: {e}")
        return 1
    if not in_path.is_file():
        emit_failure(f"input not found: {in_path}")
        return 1
    timeout_s = max(1.0, _limit(args.timeout, "DOC2KB_XLSX_TIMEOUT", DEFAULT_TIMEOUT_S, float))
    max_rss_mb = max(64, _limit(args.max_rss_mb, "DOC2KB_XLSX_MAX_RSS_MB", DEFAULT_MAX_RSS_MB, int))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sweep_stale()
    rss_note = None if _rss_kib(os.getpid()) > 0 else (
        "resource: the memory ceiling is not enforced here (process RSS cannot be read on this platform); "
        "the wall deadline is the only bound")
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            signal.signal(sig, _on_term)
    # 0700, outside the knowledge base, named after this pid for sweep_stale()
    work = Path(tempfile.mkdtemp(prefix=f"{TEMP_PREFIX}{os.getpid()}-"))
    try:
        part = work / "out.md"
        argv = [sys.executable, str(Path(__file__).resolve()), CHILD_FLAG, str(in_path), str(part),
                "--doc-id", args.doc_id, "--max-sheets", str(args.max_sheets), "--max-rows", str(args.max_rows),
                "--max-cells", str(args.max_cells), "--timeout", str(timeout_s), "--max-rss-mb", str(max_rss_mb),
                "--budget", str(round(timeout_s * SOFT_SHARE, 3)), "--work", str(work)]
        if args.source_rel:
            argv += ["--source-rel", args.source_rel]
        with open(work / "stdout", "wb") as out, open(work / "stderr", "wb") as err:
            rc, killed, seen = supervise(argv, timeout_s, max_rss_mb, out, err, work)
        child_peak = _maxrss_mib(-1)  # RUSAGE_CHILDREN
        peaks = {"parent": _maxrss_mib(0), "child": max(seen, child_peak) if child_peak is not None else seen}
        extra_warn = [rss_note] if rss_note else []
        if killed:
            why = (f"the reader ran past {timeout_s:g} s" if killed == "time"
                   else f"the reader reached {seen:g} MiB, past the {max_rss_mb} MiB ceiling")
            emit_failure(f"refused: resource: {killed} ({why})",
                         extra={"refused": True, "input": str(in_path), "peak_rss_mib": peaks, "warnings": extra_warn})
            return 1
        lines = [x for x in (work / "stdout").read_text("utf-8", "replace").splitlines() if x.startswith("{")]
        try:
            payload = json.loads(lines[-1])
        except (IndexError, ValueError):
            tail = (work / "stderr").read_text("utf-8", "replace").strip().splitlines()[-1:] or [""]
            if rc is not None and rc < 0:
                sig = signal.Signals(-rc).name if -rc in signal.valid_signals() else str(-rc)
                kind = "cpu" if sig == "SIGXCPU" else f"signal {sig}"
                emit_failure(f"refused: resource: {kind} (the reader was stopped by {sig})",
                             extra={"refused": True, "input": str(in_path), "peak_rss_mib": peaks})
            else:
                emit_failure(f"extraction failed: the reader exited {rc} without a result ({tail[0][:200]})",
                             extra={"input": str(in_path), "peak_rss_mib": peaks})
            return 1
        payload["peak_rss_mib"] = peaks
        if extra_warn:
            payload["warnings"] = list(payload.get("warnings") or []) + extra_warn
        if payload.get("ok"):
            shutil.move(str(part), str(out_path))
            payload["out"] = str(out_path)
            # Embedded objects (xl/embeddings/*) are handled here in the parent,
            # never in the openpyxl reader: they are separate documents with
            # their own extractor processes and limits (see _embedded.py).
            from _embedded import EmbedSession  # noqa: E402 — parent only
            embed = EmbedSession(input_path=in_path, doc_id=args.doc_id,
                                 source_rel=validate_source_rel(args.source_rel or in_path.name),
                                 out_path=out_path, assets_dir=resolve_assets_dir(args, out_path),
                                 assets_rel=args.assets_rel)
            embed.run_ooxml(in_path)
            embed.apply_to_file(out_path)
            if embed.warnings:
                payload["warnings"] = list(payload.get("warnings") or []) + embed.warnings
            payload.update(embed.stdout_extra())
        json_stdout(payload)
        return rc if rc in (0, 1, 2) else 1
    except _Terminated as t:
        name = signal.Signals(t.args[0]).name
        emit_failure(f"extraction failed: stopped by {name} (the reader was killed)", extra={"input": str(in_path)})
        return 128 + t.args[0]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def child_main(argv: list[str]) -> int:
    """The reader: every openpyxl call happens here, under the parent's watch and its own
    RLIMIT_CPU (so it cannot outlive a parent that died by more than a few seconds)."""
    args = _parser(child=True).parse_args(argv)
    started = time.monotonic()
    try:
        import resource
        cpu = int(math.ceil(float(args.timeout))) + 2
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    except (ImportError, ValueError, OSError):
        pass
    child_watchdog(args.lifeline, Path(args.work) if args.work else None, int(args.max_rss_mb),
                   started + float(args.timeout) + 1.0)
    in_path = Path(args.input_xlsx).resolve()
    out_path = Path(args.output_md).resolve()
    source_rel = validate_source_rel(args.source_rel or in_path.name)
    try:
        body, extras, warnings = extract(in_path, max(1, args.max_sheets), max(1, args.max_rows), max(1, args.max_cells),
                                         budget_s=args.budget)
    except Refused as e:
        emit_failure(f"refused: {e}", extra={"refused": True, "input": str(in_path)})
        return 1
    except Unprotected as e:
        emit_failure(f"refusing to parse without defusedxml: {e}", extra={"input": str(in_path)})
        return 2
    except MemoryError:
        emit_failure("refused: resource: memory (the reader ran out of memory)", extra={"refused": True, "input": str(in_path)})
        return 1
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

    tokens, exact = estimate_tokens(body, lambda: time.monotonic() > started + args.budget)
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
                                           "output_bytes", "truncated", "truncation", "row_order")})
    json_stdout(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
