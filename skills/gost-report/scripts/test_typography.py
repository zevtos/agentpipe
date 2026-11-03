#!/usr/bin/env python3
"""Tests for the opt-in typography layer and PDF export.

Assertion-based, no pytest, like the sibling test_*.py. Needs python-docx, so run it
with the skill venv:

    ~/.local/share/agentpipe/gost-report/venv/bin/python scripts/test_typography.py

What it covers:
  typograf    — non-breaking spaces, dashes, quotes, idempotence.
  emphasis    — **bold** / *italic* parsing, literal asterisks, unclosed marks.
  defaults    — without the new flags nothing changes: dashes are stripped, markup stays
                literal, no hyphenation settings; the dash validator still fails.
  typography  — Report(typography=True) writes language, hyphenation, heading spacing,
                no theme fonts; dashes stay stripped unless allow_dashes=True.
  check_lines — layout defects are found on synthetic lines.
  export      — real LibreOffice run: Times New Roman, Russian hyphens (skipped if absent).
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gost_report as G  # noqa: E402
import pdf_export  # noqa: E402
from pdf_export import Line, check_lines  # noqa: E402
from typography import NBSP, THIN, split_emphasis, typograf  # noqa: E402
import validate  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="gost-typo-")).resolve()
os.environ["GOST_REPORT_IGNORE_GLOBAL_CONFIG"] = "1"

_N = NBSP


def _xml(path: Path, part: str) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read(part).decode("utf-8")


def test_typograf():
    cases = [
        ("в доме и на улице", f"в{_N}доме и{_N}на{_N}улице"),
        ("В бак заливают", f"В{_N}бак заливают"),
        ("ранее же", f"ранее{_N}же"),
        ("3,0 л и 600 Н·м", f"3,0{_N}л и{_N}600{_N}Н·м"),
        ("100 км/ч", f"100{_N}км/ч"),
        ("387 л. с.", f"387{_N}л.{THIN}с."),
        ("и т. д.", f"и{_N}т.{THIN}д."),
        ("BMW 340i и Stage 1", f"BMW{_N}340i и{_N}Stage{_N}1"),
        ("на рисунке 3", f"на{_N}рисунке{_N}3"),
        ("А. С. Пушкин", f"А.{THIN}С.{_N}Пушкин"),
        ("Иванов И.И.", f"Иванов{_N}И.{THIN}И."),
        ("№ 5", f"№{_N}5"),
        ('слово "в кавычках"', f"слово «в{_N}кавычках»"),
        ('"внешние "вложенные" внешние"', "«внешние „вложенные“ внешние»"),
        ("ждём...", "ждём…"),
        ("по-русски и кое-что", f"по-русски и{_N}кое-что"),
    ]
    for raw, want in cases:
        assert typograf(raw) == want, (raw, typograf(raw), want)
    assert typograf("мотор - это 400-430 л. с.", dashes=True) == f"мотор{_N}— это 400–430{_N}л.{THIN}с."
    assert typograf("мотор - это 400-430") == "мотор - это 400-430"
    assert typograf("АИ-98 и 2026-11-03 и x-1-2", dashes=True) == f"АИ-98 и{_N}2026-11-03 и{_N}x-1-2"
    assert typograf("АИ-98 мотор и B58 выполнен") == f"АИ-98 мотор и{_N}B58 выполнен"
    assert typograf("от 326 до 387 л. с.") == f"от{_N}326 до{_N}387{_N}л.{THIN}с."
    assert typograf("400–430 л. и 400 В") == f"400–430{_N}л. и{_N}400{_N}В"
    assert typograf("Итог — порядка") == f"Итог{_N}— порядка"
    line_broken = "первая строка\nи вторая"
    assert "\n" in typograf(line_broken)
    for raw, _ in cases:
        once = typograf(raw, dashes=True)
        assert typograf(once, dashes=True) == once, raw


def test_emphasis():
    assert split_emphasis("a **b** c") == [("a ", False, False), ("b", True, False), (" c", False, False)]
    assert split_emphasis("*т*") == [("т", False, True)]
    assert split_emphasis("**a *b* c**") == [("a ", True, False), ("b", True, True), (" c", True, False)]
    assert split_emphasis("2 * 3 * 4") == [("2 * 3 * 4", False, False)]
    assert split_emphasis(r"цена \*5") == [("цена *5", False, False)]
    try:
        split_emphasis("**незакрыто")
    except ValueError:
        pass
    else:
        raise AssertionError("unclosed emphasis must raise")


def test_defaults_unchanged():
    r = G.Report(title_page=False, project_root=_TMP)
    r.h1("Заголовок")
    r.text("Мотор — это **важно**, в 2015 году.")
    out = r.save(_TMP / "default.docx")
    doc = _xml(out, "word/document.xml")
    assert "—" not in doc and "**важно**" in doc and NBSP not in doc
    settings = _xml(out, "word/settings.xml")
    assert "autoHyphenation" not in settings and 'w:val="14"' in settings
    assert "allow_dashes" not in out.with_name(out.name + ".gost-meta.json").read_text(encoding="utf-8")

    from docx import Document
    d = Document(str(out))
    d.paragraphs[-1].add_run(" Внезапное — тире.")
    d.save(str(out))
    assert any(v.code == "dashes" for v in validate.validate_docx(out))


def test_typography_mode():
    r = G.Report(title_page=False, project_root=_TMP, typography=True, allow_dashes=True)
    r.h1("Тюнинг двигателя")
    r.subtitle("Подзаголовок «в кавычках»\nвторая строка")
    r.h2("Раздел")
    r.text("Главное — **важная мысль** и *термин*, 400-430 л. с.")
    out = r.save(_TMP / "typo.docx")
    doc = _xml(out, "word/document.xml")
    settings = _xml(out, "word/settings.xml")
    styles = _xml(out, "word/styles.xml")

    assert NBSP in doc and "—" in doc and "400–430" in doc and "**" not in doc
    assert re.search(r"<w:b/>.*?важная мысль", doc, re.S) and re.search(r"<w:i/>.*?термин", doc, re.S)
    assert "<w:br/>" in doc
    assert 'w:val="ru-RU"' in styles and "suppressAutoHyphens" in styles
    assert "asciiTheme" not in re.search(r'w:styleId="Heading1".*?</w:style>', styles, re.S).group(0)
    assert re.search(r"defaultTabStop[^>]*/><w:autoHyphenation", settings)
    assert re.search(r'compatibilityMode"[^>]*w:val="15"', settings)
    assert '"allow_dashes": true' in out.with_name(out.name + ".gost-meta.json").read_text(encoding="utf-8")
    assert "Тюнинг" in _xml(out, "docProps/core.xml")
    assert not [v for v in validate.validate_docx(out) if v.tier == validate.TIER_FAIL]

    r = G.Report(title_page=False, project_root=_TMP, typography=True)
    r.text("Мотор — это двигатель, 400-430 л. с.")
    doc = _xml(r.save(_TMP / "typo-nodash.docx"), "word/document.xml")
    assert "—" not in doc and "–" not in doc and "400-430" in doc


def _line(page, y, words, x0=85.0, step=None):
    placed, x = [], x0
    for w in words:
        width = 8.0 * len(w)
        placed.append((w, x, x + width))
        x += width + (step or 4.0)
    return Line(page, y, y + 15.4, tuple(placed))


def _full(page, y, words, x0=85.0):
    """Строка по ширине: последнее слово упирается в правый край 500."""
    line = _line(page, y, words, x0)
    last = line.words[-1]
    shift = 500.0 - last[2]
    return Line(page, y, y + 15.4, line.words[:-1] + ((last[0], last[1] + shift, 500.0),))


def _codes(lines):
    return [i.code for i in check_lines(lines)]


def test_check_lines():
    edge = _full(1, 90, ["правый", "край"])
    assert "hanging-word" in _codes([_full(1, 100, ["строка", "и"]), edge])
    assert "broken-range" in _codes([_full(1, 100, ["мощность", "400–"]), edge])
    assert "dash-at-start" in _codes([edge, _full(1, 120, ["—", "тире", "в", "начале"])])
    assert "runt" in _codes([_full(1, 100, ["длинная", "строка"]), _line(1, 120, ["хвост"])])
    assert "wide-gap" in _codes([_full(1, 100, ["две", "дыра"])])
    assert "widow" in _codes([_full(1, 700, ["строка", "текста"]), _line(2, 60, ["короткая", "концовка", "абзаца", "здесь"])])
    assert "orphan" in _codes([edge, _full(1, 700, ["начало", "абзаца"], x0=120.0), _full(2, 60, ["продолжение", "абзаца"])])
    assert check_lines([_full(1, 100, ["ровная", "строка", "без", "дефектов", "абсолютнейше", "нормальнейшего"])]) == []


def test_export():
    try:
        pdf_export._find_soffice()
    except RuntimeError:
        print("SKIP export: LibreOffice не установлен")
        return
    if not shutil.which("pdffonts"):
        print("SKIP export: нет poppler")
        return
    r = G.Report(title_page=False, project_root=_TMP, typography=True, allow_dashes=True)
    r.h1("Проверка экспорта")
    r.text("Экспериментально подтверждённая электропроводность высокотемпературных "
           "сверхпроводников существенно ограничивается теплопроводностью подложки. " * 6)
    r.save(_TMP / "export.docx")
    pdf = r.export_pdf()
    assert pdf == _TMP / "export.pdf" and pdf.exists()
    fonts = subprocess.run(["pdffonts", str(pdf)], capture_output=True, text=True).stdout
    assert "TimesNewRoman" in fonts and "Liberation" not in fonts and "Calibri" not in fonts, fonts
    text = subprocess.run(["pdftotext", "-raw", str(pdf), "-"], capture_output=True, text=True).stdout
    assert re.search(r"[а-яё]-\n", text), "русских переносов нет"
    assert not [i for i in pdf_export.check_pdf(pdf, font=G.FONT_NAME) if i.code in ("font", "hanging-word")]

    # Неразрывные пробелы тянутся вместе с обычными: профиль с UseVariableWidthNBSP.
    r = G.Report(title_page=False, project_root=_TMP, typography=True)
    r.text("В доме и на улице, в поле и на реке, в 2015 году и в 2016 году, с утра и до вечера. " * 4)
    r.save(_TMP / "nbsp.docx")
    lines = pdf_export._read_lines(r.export_pdf(check=False))
    right = max(l.x1 for l in lines)
    for line in lines:
        gaps = [b[1] - a[2] for a, b in zip(line.words, line.words[1:])]
        if line.x1 >= right - 1.5 and len(gaps) > 3:
            assert max(gaps) - min(gaps) < 1.5, (gaps, line.tokens)

    # Номер страницы не наследует полужирный и кегль основного текста.
    html = subprocess.run(["pdftohtml", "-xml", "-stdout", "-i", str(pdf)], capture_output=True, text=True).stdout
    assert re.search(r'<text [^>]*height="1\d"[^>]*>1</text>', html), "номер страницы не 11 пт"

    r = G.Report(title_page=False, project_root=_TMP)
    r.text("Без сохранения.")
    try:
        r.export_pdf()
    except RuntimeError:
        pass
    else:
        raise AssertionError("export_pdf before save must raise")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok ", t.__name__)
    shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\nOK  all {len(tests)} typography tests passed")
