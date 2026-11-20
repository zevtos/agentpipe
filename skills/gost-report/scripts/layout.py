"""layout — проход вёрстки LibreOffice для того, что зависит от страниц.

.docx не знает, где кончается страница: это решает программа, которая его
открывает. Два требования положений о ВКР без этого не выполнить:

* «Продолжение таблицы N» над частью таблицы, перенесённой на следующую
  страницу (ГОСТ 7.32 п. 6.6.5, положение МБИ п. 10.10.3);
* число страниц в строке объёма реферата.

r.save() собирает .docx, конвертирует копию в PDF тем же LibreOffice, что и
r.export_pdf(), и смотрит, на какой странице оказалась каждая строка таблиц
с continuation (в строки на время прохода ставятся невидимые метки). Таблица
режется перед первой строкой новой страницы: над второй частью — «Продолжение
таблицы N» с новой страницы, шапка повторяется, у первой части нет нижней линии.
Проход повторяется, пока таблицы не перестанут рваться (часть могла снова
не влезть). Word считает страницы чуть иначе, поэтому разрыв ставится явно.

Без LibreOffice или poppler (pdftotext) проход пропускается с предупреждением:
шапка таблицы всё равно повторяется (tblHeader), поле NUMPAGES Word обновит
при открытии.
"""
from __future__ import annotations

import copy
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

MAX_PASSES = 12
_MARK = "QZT{t}R{r}QZ"
_MARK_RE = re.compile(r"QZT(\d+)R(\d+)QZ")


def _warn(message: str) -> None:
    sys.stderr.write(f"gost-report layout: {message}\n")


def _pdf_pages_text(docx: Path, tmp: Path) -> Optional[List[str]]:
    """Текст PDF по страницам (LibreOffice → pdftotext). None — нечем."""
    import pdf_export
    pdftotext = shutil.which("pdftotext")
    if pdftotext is None:
        _warn("нет pdftotext (poppler): проход вёрстки пропущен. "
              "brew install poppler / sudo apt install poppler-utils")
        return None
    pdf = pdf_export.export_pdf(docx, tmp / (docx.stem + ".pdf"))
    res = subprocess.run([pdftotext, "-layout", str(pdf), "-"],
                         capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        _warn(f"pdftotext упал: {res.stderr.strip()[-200:]}")
        return None
    pages = res.stdout.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def run_layout_pass(report, target: Path, *, pages: bool, required: bool) -> None:
    try:
        import pdf_export
        pdf_export._find_soffice()
    except RuntimeError:
        if required:
            raise
        what = []
        if report._continuation:
            what.append("«Продолжение таблицы N» не расставлено (шапка повторяется)")
        if pages:
            what.append("число страниц в строке объёма обновит Word при открытии")
        _warn(f"LibreOffice не найден: {'; '.join(what)}.")
        return
    with tempfile.TemporaryDirectory(prefix="gost-layout-") as tmp_name:
        tmp = Path(tmp_name)
        if report._continuation:
            _continuations(report, target, tmp)
        if pages:
            text = _pdf_pages_text(target, tmp)
            if text is not None:
                report.set_page_count(len(text))
                report._write(target)


def _first_paragraph(tr):
    tc = tr.find(qn("w:tc"))
    if tc is None:
        return None
    return tc.find(qn("w:p"))


def _marker_run(text: str):
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "FFFFFF")
    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), "2")
    rpr.append(color)
    rpr.append(sz)
    run.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    run.append(t)
    return run


def _continuations(report, target: Path, tmp: Path) -> None:
    # Строка, разорванная между страницами, ломает разбиение: она осталась бы
    # хвостом на странице с «Продолжением». Поэтому строки таких таблиц целые.
    for tbl, _number, _head in report._continuation:
        for tr in tbl.findall(qn("w:tr")):
            trpr = tr.find(qn("w:trPr"))
            if trpr is None:
                trpr = OxmlElement("w:trPr")
                tcs = tr.find(qn("w:tblPrEx"))
                (tcs.addnext(trpr) if tcs is not None else tr.insert(0, trpr))
            if trpr.find(qn("w:cantSplit")) is None:
                trpr.insert(0, OxmlElement("w:cantSplit"))
    for attempt in range(MAX_PASSES):
        marks: List[Tuple[object, object]] = []
        for ti, (tbl, _number, head) in enumerate(report._continuation):
            for ri, tr in enumerate(tbl.findall(qn("w:tr"))):
                if ri < head:
                    continue
                p = _first_paragraph(tr)
                if p is None:
                    continue
                run = _marker_run(_MARK.format(t=ti, r=ri))
                p.append(run)
                marks.append((p, run))
        probe = tmp / f"probe-{attempt}.docx"
        report._write(probe)
        for p, run in marks:
            p.remove(run)
        pages = _pdf_pages_text(probe, tmp)
        if pages is None:
            return
        where: Dict[Tuple[int, int], int] = {}
        for page_no, page in enumerate(pages):
            for m in _MARK_RE.finditer(page):
                where.setdefault((int(m.group(1)), int(m.group(2))), page_no)
        splits = []
        for ti, (tbl, _number, head) in enumerate(report._continuation):
            rows = [(ri, where.get((ti, ri))) for ri in range(head, len(tbl.findall(qn("w:tr"))))]
            rows = [(ri, pg) for ri, pg in rows if pg is not None]
            if not rows:
                continue
            first_page = rows[0][1]
            for ri, pg in rows[1:]:
                if pg > first_page:
                    splits.append((ti, ri))
                    break
        if not splits:
            report._write(target)
            return
        for ti, ri in splits:
            _split(report, ti, ri)
    _warn(f"«Продолжение таблицы» не сошлось за {MAX_PASSES} проходов; "
          "проверь таблицы вручную.")
    report._write(target)


def _no_bottom_border(tr) -> None:
    for tc in tr.findall(qn("w:tc")):
        tcpr = tc.find(qn("w:tcPr"))
        if tcpr is None:
            tcpr = OxmlElement("w:tcPr")
            tc.insert(0, tcpr)
        borders = tcpr.find(qn("w:tcBorders"))
        if borders is None:
            borders = OxmlElement("w:tcBorders")
            tcpr.insert_element_before(
                borders, "w:shd", "w:noWrap", "w:tcMar", "w:textDirection",
                "w:tcFitText", "w:vAlign", "w:hideMark", "w:headers",
                "w:cellIns", "w:cellDel", "w:cellMerge", "w:tcPrChange")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "nil")
        borders.append(bottom)


def _split(report, index: int, row: int) -> None:
    tbl, number, head = report._continuation[index]
    rows = tbl.findall(qn("w:tr"))
    if row <= head or row >= len(rows):
        return
    prof = report.profile
    new_tbl = OxmlElement("w:tbl")
    for child in tbl:
        if child.tag in (qn("w:tblPr"), qn("w:tblGrid")):
            new_tbl.append(copy.deepcopy(child))
    for tr in rows[:head]:
        new_tbl.append(copy.deepcopy(tr))
    for tr in rows[row:]:
        new_tbl.append(tr)
    _no_bottom_border(rows[row - 1])

    from gost_report import _align_const, _suppress_hyphens
    label = report.make_paragraph(
        align=_align_const(prof.table_caption_align),
        first_line_indent=(Cm(prof.table_caption_indent_cm)
                           if prof.table_caption_indent_cm else Pt(0)),
        line_spacing=prof.caption_line_spacing)
    run = label.add_run(prof.table_continuation_text.format(n=number))
    report.set_run_font(run, size=Pt(prof.caption_font_size) if prof.caption_font_size else None)
    pf = label.paragraph_format
    pf.keep_with_next = True
    pf.page_break_before = True
    if not prof.caption_hyphenation:
        _suppress_hyphens(label._p.get_or_add_pPr())
    tbl.addnext(label._p)
    label._p.addnext(new_tbl)
    report._continuation.append((new_tbl, number, head))
