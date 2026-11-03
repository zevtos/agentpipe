#!/usr/bin/env python3
"""Integration tests for the profile-driven layout knobs (school / non-GOST rules).

Builds real .docx files through Report and inspects the saved XML. Requires
python-docx — run through the skill venv:

    python3 scripts/ensure_env.py scripts/test_profile_layout.py

What it covers:
  typography  — body_font_size / line_spacing / first_line_indent_cm /
                heading_space_after_pt reach Normal, headings and body text.
  page number — page_number_position puts PAGE in the header or footer with
                the right alignment; the title page shows none; the first
                body page (TOC) DOES (regression: add_section() used to copy
                <w:titlePg/> into the body section and blank its first page).
  title page  — title_student_layout="label_group" renders
                «Выполнила: <group>» with names below; title_builder replaces
                the built-in title layout.
  validation  — a typo in page_number_position / title_student_layout raises.
  h1 breaks   — h1_new_page sets pageBreakBefore on the Heading 1 paragraph
                instead of a break-only paragraph before it (regression: that
                paragraph slid onto the next page and left it blank); none
                after toc() / page_break(), none with h1_new_page=False.
  env         — GOST_REPORT_IGNORE_GLOBAL_CONFIG=1 in a subject .gost-report.env
                stops the owner's global config from leaking onto the title.

Exit codes: 0 all passed, 1 a test failed, 2 python-docx missing.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    import docx  # noqa: F401
except ImportError as e:
    sys.stderr.write(f"SKIP-FATAL: python-docx not available ({e}).\n")
    raise SystemExit(2)

import gost_report as G  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="gost-profile-test-"))
(_TMP / ".gost-report").mkdir()          # project marker → paths() resolves here
_PASSED = 0
_FAILURES: list = []

SCHOOL = G.UniversityProfile(
    title_margin_left=20, title_margin_right=20,
    title_margin_top=20, title_margin_bottom=20,
    body_margin_left=20, body_margin_right=20,
    body_margin_top=20, body_margin_bottom=20,
    body_font_size=12, first_line_indent_cm=1.0, heading_space_after_pt=0,
    heading_size_h1=12, heading_size_h2=12, heading_size_h3=12,
    heading_align_h2="center", heading_align_h3="center",
    h1_uppercase=False, page_number_position="top-right", page_number_size=12,
    title_size_large=12, title_student_layout="label_group",
    title_group_template="{group}", ministry="", toc_title="Оглавление",
    university_short="«Гимназия»", city="г. Город",
)

TITLE = dict(work_type="Индивидуальный проект", topic="Тема",
             student_label="Выполнила", student_group="ученица 9 «Б» класса",
             student_name="Иванова Анна Петровна", teacher_label="Руководитель",
             teacher_name="Петрова Мария Ивановна", year="2026")


def check(name, fn):
    global _PASSED
    try:
        fn()
        _PASSED += 1
        print(f"ok    {name}")
    except Exception:
        _FAILURES.append(f"FAIL  {name}\n{traceback.format_exc()}")
        print(f"FAIL  {name}")


def _build(profile, name, **title):
    r = G.Report(G.TitleConfig(**{**TITLE, **title}), profile=profile,
                 project_root=_TMP)
    r.toc()
    r.h1("Введение")
    r.text("Абзац основного текста.")
    r.h2("§1. Параграф")
    return zipfile.ZipFile(r.save(_TMP / f"{name}.docx"))


def _xml(z, part):
    return z.read(part).decode("utf-8")


def _style(styles_xml, style_id):
    m = re.search(r'<w:style [^>]*w:styleId="%s".*?</w:style>' % style_id,
                  styles_xml, re.S)
    assert m, style_id
    return m.group(0)


def test_typography():
    z = _build(SCHOOL, "typo")
    styles = _xml(z, "word/styles.xml")
    assert '<w:sz w:val="24"/>' in _style(styles, "Normal")
    h1 = _style(styles, "Heading1")
    assert '<w:sz w:val="24"/>' in h1 and 'w:after="0"' in h1
    assert '<w:jc w:val="center"/>' in _style(styles, "Heading2")
    doc = _xml(z, "word/document.xml")
    assert '<w:ind w:firstLine="567"/>' in doc, "1 cm first-line indent"
    sizes = set(re.findall(r'<w:sz w:val="(\d+)"', doc))
    assert sizes == {"24"}, f"everything 12 pt, got {sizes}"
    margins = re.findall(r'<w:pgMar [^>]*>', doc)
    assert margins and all('w:left="1134"' in m and 'w:right="1134"' in m
                           for m in margins), margins
    assert "Введение" in doc and "ВВЕДЕНИЕ" not in doc


def test_page_number_header_right():
    z = _build(SCHOOL, "pn")
    doc = _xml(z, "word/document.xml")
    assert "w:footerReference" not in doc
    sects = re.findall(r'<w:sectPr.*?</w:sectPr>', doc, re.S)
    assert len(sects) == 2
    assert "<w:titlePg" in sects[0], "title page number hidden"
    assert "<w:titlePg" not in sects[1], "TOC page must keep its number"
    numbered = [n for n in z.namelist() if n.startswith("word/header")
                and "PAGE" in _xml(z, n)]
    assert numbered and all('<w:jc w:val="right"/>' in _xml(z, n)
                            for n in numbered)


def test_default_profile_keeps_footer_center():
    z = _build(G.GOST_PROFILE, "gost", student_label="", student_group="P3100",
               teacher_label="Проверил")
    doc = _xml(z, "word/document.xml")
    assert "w:headerReference" not in doc
    footers = [n for n in z.namelist() if n.startswith("word/footer")
               and "PAGE" in _xml(z, n)]
    assert footers and all('<w:jc w:val="center"/>' in _xml(z, n)
                           for n in footers)
    sects = re.findall(r'<w:sectPr.*?</w:sectPr>', doc, re.S)
    assert "<w:titlePg" not in sects[1]


def test_label_group_title():
    z = _build(SCHOOL, "title")
    doc = _xml(z, "word/document.xml")
    text = [("".join(re.findall(r'<w:t[^>]*>([^<]*)', p)))
            for p in re.findall(r'<w:p[ >].*?</w:p>', doc, re.S)]
    i = text.index("Выполнила: ученица 9 «Б» класса")
    assert text[i + 1] == "Иванова Анна Петровна"
    assert "Группа" not in doc
    assert "Министерство" not in doc


def test_title_builder():
    def builder(r):
        r.add_text_paragraph("СВОЙ ТИТУЛ", bold=True)
        r.add_bottom_block([r.title_config.year])

    from dataclasses import replace
    z = _build(replace(SCHOOL, title_builder=builder), "builder")
    doc = _xml(z, "word/document.xml")
    assert "СВОЙ ТИТУЛ" in doc and "Выполнила" not in doc


def test_bad_values_raise():
    from dataclasses import replace
    for bad in (dict(page_number_position="top-middle"),
                dict(title_student_layout="inline")):
        try:
            G.Report(G.TitleConfig(**TITLE), profile=replace(SCHOOL, **bad),
                     project_root=_TMP)
        except ValueError:
            continue
        raise AssertionError(f"{bad} must raise ValueError")


def _h1_layout(profile, name):
    """[(text, page_break_before, previous paragraph is break-only)] по h1."""
    r = G.Report(G.TitleConfig(**TITLE), profile=profile, project_root=_TMP)
    r.toc()
    r.h1("Введение")
    r.text("Абзац.")
    r.h1("1 Раздел")
    r.text("Абзац.")
    r.page_break()
    r.h1("2 Раздел")
    r.text("Абзац.")
    doc = _xml(zipfile.ZipFile(r.save(_TMP / f"{name}.docx")), "word/document.xml")
    paras = re.findall(r'<w:p[ >].*?</w:p>', doc, re.S)
    break_only = ['w:type="page"' in p and "<w:t" not in p for p in paras]
    return [("".join(re.findall(r'<w:t[^>]*>([^<]*)', p)),
             "<w:pageBreakBefore/>" in p, i > 0 and break_only[i - 1])
            for i, p in enumerate(paras) if '<w:pStyle w:val="Heading1"/>' in p]


def test_h1_page_break_before():
    assert _h1_layout(SCHOOL, "h1-break") == [
        ("Введение", False, True),     # TOC page ends with its own break
        ("1 Раздел", True, False),     # property on the heading, no break paragraph
        ("2 Раздел", False, True),     # explicit page_break(): no double break
    ]
    from dataclasses import replace
    assert _h1_layout(replace(SCHOOL, h1_new_page=False), "h1-flow") == [
        ("Введение", False, True),
        ("1 Раздел", False, False),
        ("2 Раздел", False, True),
    ]


def test_ignore_global_config():
    cfg = _TMP / "global.env"
    cfg.write_text('GOST_REPORT_STUDENT_NAME="Владелец В.В."\n'
                   'GOST_REPORT_STUDENT_GROUP="P0000"\n', encoding="utf-8")
    subject = _TMP / ".gost-report.env"
    old = os.environ.get("GOST_REPORT_CONFIG")
    os.environ["GOST_REPORT_CONFIG"] = str(cfg)
    try:
        G._caller_file = lambda: _TMP / ".gost-report" / "build.py"
        leaked = G._apply_env_overrides(G.TitleConfig(**TITLE))
        assert leaked.student_name == "Владелец В.В.", "control: global applies"
        subject.write_text("GOST_REPORT_IGNORE_GLOBAL_CONFIG=1\n", encoding="utf-8")
        kept = G._apply_env_overrides(G.TitleConfig(**TITLE))
        assert kept.student_name == TITLE["student_name"]
        assert kept.student_group == TITLE["student_group"]
    finally:
        subject.unlink(missing_ok=True)
        if old is None:
            os.environ.pop("GOST_REPORT_CONFIG", None)
        else:
            os.environ["GOST_REPORT_CONFIG"] = old


def main() -> int:
    # Изоляция от личного ~/.config/gost-report/config разработчика.
    os.environ["GOST_REPORT_CONFIG"] = str(_TMP / "absent.env")
    check("typography: 12 pt, 1 cm indent, 0 pt after headings, 2 cm margins",
          test_typography)
    check("page number: header right, hidden on title, shown on TOC page",
          test_page_number_header_right)
    check("default profile: footer center, body section without titlePg",
          test_default_profile_keeps_footer_center)
    check("title: label_group layout", test_label_group_title)
    check("title: title_builder replaces built-in layout", test_title_builder)
    check("validation: bad position / layout raise", test_bad_values_raise)
    check("h1: page_break_before on the heading, no break paragraph",
          test_h1_page_break_before)
    check("env: IGNORE_GLOBAL_CONFIG blocks owner's global config",
          test_ignore_global_config)

    total = _PASSED + len(_FAILURES)
    if _FAILURES:
        sys.stderr.write("\n" + "\n".join(_FAILURES))
        sys.stderr.write(f"\n{len(_FAILURES)}/{total} test(s) FAILED.\n")
        return 1
    print(f"\nOK  all {total} profile layout tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
