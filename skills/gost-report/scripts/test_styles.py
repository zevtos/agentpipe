#!/usr/bin/env python3
"""Integration tests for style files and the profile knobs added for
university regulations other than ITMO (МБИ and the like).

Builds real .docx files through Report and inspects the saved XML. Requires
python-docx — run through the skill venv:

    python3 scripts/ensure_env.py scripts/test_styles.py

What it covers:
  styles      — load_profile: built-in «mbi», extends + sections, JSON, overrides,
                unknown key / bad type / extends cycle errors, dump_toml round trip,
                every UniversityProfile field documented in styles.FIELD_DOCS.
  defaults    — ITMO/GOST output keeps «—» captions, template lists, no hyphenation.
  mbi         — «–» in captions and notes, hyphenation off in headings/captions,
                h2 from the paragraph indent, justified, one line before/after,
                12 pt tables with repeated header and unsplittable rows, «1)» and «–»
                lists on the paragraph indent, «а)» letters, required NBSP, keywords,
                volume line, appendix «А.1», title page from [[title.blocks]].
  numbering   — numbering="chapter" + heading_numbering: «1», «1.1», «Рисунок 1.1»,
                «(2.1)», forward references by label.
  markdown    — r.markdown: headings, lists, pipe table, citation, forward refs.
  title_docx  — a .docx blank: fields split across runs, logo copied, no FAIL.
  validator   — MBI rules: «–» passes, «—» and « - » fail; WARN checks fire.
  continuation— with LibreOffice + poppler: long table gets «Продолжение таблицы N».

Exit codes: 0 all passed, 1 a test failed, 2 python-docx missing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import sys
import tempfile
import traceback
import zipfile
import zlib
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
import styles  # noqa: E402
import validate as V  # noqa: E402
from typography import NBSP  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="gost-styles-test-"))
(_TMP / ".gost-report").mkdir()
_PASSED = 0
_FAILURES: list = []

TITLE = dict(topic="Учёт обращений граждан", student_name="Быстров Денис Максимович",
             student_group="324-ИТвБ", year="2027", course="4", study_form="очной",
             department="прикладной информатики", teacher_name="Барабанова Марина Ивановна",
             teacher_degree="канд. экон. наук", teacher_position="доцент")


def check(name, fn):
    global _PASSED
    try:
        fn()
        _PASSED += 1
        print(f"ok    {name}")
    except Exception:
        _FAILURES.append(f"FAIL  {name}\n{traceback.format_exc()}")
        print(f"FAIL  {name}")


def _png(path: Path) -> Path:
    raw = b"".join(b"\x00" + bytes([90, 90, 90] * 30) for _ in range(15))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", 30, 15, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


IMG = None


def _xml(path: Path, part: str = "word/document.xml") -> str:
    with zipfile.ZipFile(path) as z:
        return z.read(part).decode("utf-8")


def _texts(path: Path) -> list:
    doc = _xml(path)
    return ["".join(re.findall(r"<w:t[^>]*>([^<]*)", p))
            for p in re.findall(r"<w:p[ >].*?</w:p>", doc, re.S)]


def _plain(texts: list) -> list:
    """Тексты без неразрывных пробелов: профиль МБИ ставит их сам."""
    return [t.replace(NBSP, " ") for t in texts]


def _style(styles_xml: str, style_id: str) -> str:
    m = re.search(r'<w:style [^>]*w:styleId="%s".*?</w:style>' % style_id, styles_xml, re.S)
    assert m, style_id
    return m.group(0)


# ---------------------------------------------------------------------------

def test_style_loading():
    mbi = G.load_profile("mbi")
    assert mbi.name == "mbi" and mbi.dash == "–" and mbi.table_font_size == 12
    assert mbi.heading_align_h2 == "justify" and mbi.heading_space_before_h2 == "1line"
    assert mbi.bib_style == "7.0.100-2018" and mbi.title_blocks
    assert G.MBI_PROFILE.dash == "–"            # lazy module attribute

    (_TMP / "child.toml").write_text(
        'extends = "mbi"\nnumbering = "chapter"\n[h2]\nsize = 15\n'
        '[table]\nfont_size = 11\n[page]\nmargin_left = 25\n', encoding="utf-8")
    child = G.load_profile(str(_TMP / "child.toml"))
    assert (child.numbering, child.heading_size_h2, child.table_font_size,
            child.body_margin_left, child.dash) == ("chapter", 15, 11, 25, "–")
    assert child.name == "child" and child.base_dir == str(_TMP.resolve())

    (_TMP / "j.json").write_text(json.dumps({"extends": "itmo", "body": {"font_size": 12}}),
                                 encoding="utf-8")
    j = G.load_profile(str(_TMP / "j.json"), toc_title="СОДЕРЖАНИЕ")
    assert j.body_font_size == 12 and j.toc_title == "СОДЕРЖАНИЕ"
    assert j.heading_size_h2 == 15                              # from ITMO

    for body, needle in (('[table]\nfontsize = 12\n', "font_size"),
                         ('heading_alignh2 = "left"\n', "heading_align_h2"),
                         ('[table]\nfont_size = "big"\n', "число"),
                         ('nbsp = "sometimes"\n', "nbsp")):
        bad = _TMP / "bad.toml"
        bad.write_text(body, encoding="utf-8")
        try:
            G.load_profile(str(bad))
        except ValueError as e:
            assert needle in str(e), (needle, str(e))
        else:
            raise AssertionError(f"{body!r} must raise")
    (_TMP / "a.toml").write_text('extends = "b.toml"\n', encoding="utf-8")
    (_TMP / "b.toml").write_text('extends = "a.toml"\n', encoding="utf-8")
    try:
        G.load_profile(str(_TMP / "a.toml"))
    except ValueError as e:
        assert "цикл" in str(e)
    else:
        raise AssertionError("extends cycle must raise")
    try:
        G.load_profile("no-such-style")
    except LookupError:
        pass
    else:
        raise AssertionError("unknown style must raise LookupError")

    dumped = _TMP / "dump.toml"
    dumped.write_text(styles.dump_toml(mbi), encoding="utf-8")
    again = G.load_profile(str(dumped))
    for f in ("dash", "prose_dashes", "nbsp", "heading_space_after_h1", "table_repeat_header",
              "list_layout", "numbered_format", "bib_style", "title_blocks", "checks"):
        assert getattr(again, f) == getattr(mbi, f), f
    assert styles.check_docs() == [], styles.check_docs()


def test_defaults_untouched():
    r = G.Report(title_page=False, profile=G.GOST_PROFILE, project_root=_TMP)
    r.h2("Раздел")
    r.figure(IMG, "Схема системы целиком")
    r.table([["a", "b"], ["1", "2"]], caption="Данные опыта")
    r.numbered(["раз", "два"])
    out = r.save(_TMP / "gost.docx")
    texts = _texts(out)
    assert "Рисунок 1 — Схема системы целиком" in texts
    assert "Таблица 1 — Данные опыта" in texts
    settings = _xml(out, "word/settings.xml")
    assert "autoHyphenation" not in settings and 'w:val="14"' in settings
    numbering = _xml(out, "word/numbering.xml")
    assert 'w:abstractNumId="60"' not in numbering, "new list engine must stay off"
    doc = _xml(out)
    assert "w:tblHeader" not in doc and "w:cantSplit" not in doc
    assert "<w:sz w:val=\"28\"/>" in doc


def _mbi_doc(name="mbi.docx", **over):
    r = G.Report(G.TitleConfig(**TITLE), profile=G.load_profile("mbi", **over),
                 project_root=_TMP)
    r.h1("Реферат", toc=False)
    r.volume_line()
    r.keywords(["учёт обращений", "документооборот", "bpmn", "автоматизация", "администрация"])
    r.text("Объект исследования - процессы учёта. Руководитель Т. Г. Пискунова, "
           "объём 45–60 листов, в 2026 г. Данные в таблице 1, схема на рисунке 1.")
    r.toc()
    r.h1("Введение")
    r.text("изучить процесс и внедрить систему.", lead="Задачи.")
    r.numbered(["изучить процесс;", "внедрить систему."])
    r.bullet(["первый;", "второй."])
    r.lettered(["требования;", ("контроль:", ["срок;", "исполнитель."])])
    r.h1("1 Анализ")
    r.h2("1.1 Процесс")
    r.table([["Этап", "Срок, дн."], ["Регистрация", "3"]], caption="Этапы", widths=[10, 6],
            note="Сроки по закону.")
    r.figure(IMG, "Схема процесса учёта", explication="1 – регистрация; 2 – ответ")
    r.formula(r"\rho = \frac{m}{V}", where=[("m", "масса, кг"), ("V", "объём, м³")])
    r.note("Значения округлены.")
    r.note(["Первое.", "Второе."])
    a = r.appendix("Форма журнала", "справочное")
    r.text(f"Форма в таблице {a}.1.")
    r.table([["Поле", "Тип"], ["Дата", "дата"]], caption="Поля журнала")
    return r.save(_TMP / name, layout=False)


def test_mbi_document():
    out = _mbi_doc()
    texts = _texts(out)
    doc = _xml(out)
    styles_xml = _xml(out, "word/styles.xml")
    settings = _xml(out, "word/settings.xml")
    numbering = _xml(out, "word/numbering.xml")
    assert "Таблица 1 – Этапы" in texts and "Рисунок 1 – Схема процесса учёта" in texts
    assert "Таблица А.1 – Поля журнала" in texts
    assert "Примечание – Значения округлены." in texts and "Примечания:" in texts
    assert any(t.startswith("Объект исследования – процессы учёта") for t in texts)
    assert any(f"Т.{NBSP}Г.{NBSP}Пискунова" in t for t in texts), "required nbsp"
    assert any("45–60" in t for t in texts)
    assert "УЧЁТ ОБРАЩЕНИЙ, ДОКУМЕНТООБОРОТ, BPMN, АВТОМАТИЗАЦИЯ, АДМИНИСТРАЦИЯ" in texts
    vol = next(t for t in texts if t.startswith("Выпускная квалификационная работа содержит"))
    assert "1 рис., 2 табл., 0 источников, 1 прил." in vol, vol
    assert "ПРИЛОЖЕНИЕ А(справочное)Форма журнала" in texts
    assert any(t.startswith("а) ") for t in texts) and any(t.startswith("б) ") for t in texts)
    assert "<w:autoHyphenation w:val=\"true\"/>" in settings
    assert 'w:lang w:val="ru-RU"' in styles_xml
    h2 = _style(styles_xml, "Heading2")
    assert all(x in h2 for x in ('<w:jc w:val="both"/>', 'w:firstLine="709"',
                                 'w:before="480"', 'w:after="480"', "suppressAutoHyphens"))
    h1 = _style(styles_xml, "Heading1")
    assert 'w:before="0"' in h1 and 'w:after="480"' in h1
    assert "Heading1Unlisted" in doc and "<w:outlineLvl w:val=\"9\"/>" in styles_xml
    assert doc.count("<w:tblHeader/>") >= 2 and "<w:cantSplit/>" in doc
    assert re.search(r'<w:tc>.*?<w:sz w:val="24"/>.*?</w:tc>', doc, re.S), "12 pt cells"
    assert '<w:lvlText w:val="%1)"/>' in numbering and '<w:lvlText w:val="–"/>' in numbering
    assert numbering.count('<w:suff w:val="space"/>') >= 2
    cap = re.search(r"<w:p>(?:(?!</w:p>).)*Рисунок 1 – Схема(?:(?!</w:p>).)*</w:p>", doc, re.S)
    assert cap and "suppressAutoHyphens" in cap.group(0)
    assert "Пискунова" not in "".join(texts[:3])
    title = texts[:40]
    assert "«Допущена к защите»" in title and "ВЫПУСКНАЯ КВАЛИФИКАЦИОННАЯ РАБОТА" in title
    assert "Кафедра прикладной информатики" in title
    assert "канд. экон. наук, доцент" in title
    fails = [v for v in V.validate_docx(out) if v.tier == V.TIER_FAIL]
    assert not fails, fails


def test_chapter_numbering():
    prof = G.load_profile("mbi", numbering="chapter", heading_numbering=True)
    r = G.Report(title_page=False, profile=prof, project_root=_TMP)
    r.h1("Введение")
    r.text(f"Схема на {r.ref.figure('fig:a')}, данные {r.ref.in_table('tbl:b')}.")
    r.h1("Анализ")
    r.h2("Процесс")
    n = r.figure(IMG, "Первый рисунок главы", label="fig:a")
    assert n == "1.1" and isinstance(n, G.RefNumber)
    r.h2("Выводы по главе 1")
    r.h1("Проектирование")
    t = r.table([["a", "b"], ["1", "2"]], caption="Таблица главы", label="tbl:b")
    f = r.formula(r"y = kx")
    assert (t, f) == ("2.1", "2.1")
    r.text(f"По формуле ({f}) и {r.ref.by_formula(f)}.")
    r.h1("Заключение")
    out = r.save(_TMP / "chapters.docx", layout=False)
    texts = _plain(_texts(out))
    for want in ("ВВЕДЕНИЕ", "1 АНАЛИЗ", "1.1 Процесс", "Выводы по главе 1",
                 "2 ПРОЕКТИРОВАНИЕ", "ЗАКЛЮЧЕНИЕ", "Рисунок 1.1 – Первый рисунок главы",
                 "Таблица 2.1 – Таблица главы", "Схема на рисунок 1.1, данные в таблице 2.1."):
        assert want in texts, (want, texts)
    assert any("(2.1)" in t for t in texts)
    assert not [v for v in V.validate_docx(out) if v.tier == V.TIER_FAIL]


def test_markdown():
    r = G.Report(title_page=False, profile="mbi", project_root=_TMP)
    r.bib.add("iv", type="book", authors=["Иванов И. И."], title="Книга", city="М.",
              publisher="Юрайт", year=2020, pages=100)
    r.markdown("""
# Введение

**Тема.** Работа - про учёт, см. таблицу @tbl:t и источник [@iv].

1) первый;
2) второй:
   а) вложенный;
3) третий.

Таблица: Сроки {#tbl:t widths=8,8}
| Этап | Срок, дн. |
|:-----|:---------:|
| Регистрация | 3 |

> Примечание к таблице.

\\references
""")
    out = r.save(_TMP / "md.docx", layout=False)
    texts = _plain(_texts(out))
    assert "Тема. Работа – про учёт, см. таблицу 1 и источник [1]." in texts, texts
    assert "Таблица 1 – Сроки" in texts and "а) вложенный;" in texts
    assert "Примечание – Примечание к таблице." in texts
    assert any(t.startswith("1. Иванов И. И. Книга") for t in texts)
    doc = _xml(out)
    assert re.search(r'<w:jc w:val="center"/>(?:(?!</w:p>).)*Срок, дн\.', doc, re.S)


def test_title_docx():
    from docx import Document
    from docx.shared import Cm
    blank = Document()
    blank.sections[0].left_margin = Cm(2.5)
    blank.add_paragraph().add_run().add_picture(str(IMG), width=Cm(2))
    p = blank.add_paragraph()
    for piece in ("Тема: «", "{to", "pic}", "»"):
        p.add_run(piece)
    blank.add_paragraph("Студент {student_name}, руководитель {teacher_name|______}")
    path = _TMP / "blank.docx"
    blank.save(path)
    prof = G.load_profile("gost", title_docx=str(path))
    r = G.Report(G.TitleConfig(topic="Учёт", student_name="Иванов И. И.", year="2026"),
                 profile=prof, project_root=_TMP)
    r.h1("Введение")
    out = r.save(_TMP / "title_docx.docx", layout=False)
    texts = _texts(out)
    assert "Тема: «Учёт»" in texts and "Студент Иванов И. И., руководитель ______" in texts
    assert "<w:drawing>" in _xml(out)
    assert not [v for v in V.validate_docx(out) if v.tier == V.TIER_FAIL]


def test_validator_rules():
    out = _mbi_doc("mbi_val.docx")
    style = json.loads((out.parent / (out.name + ".gost-meta.json")).read_text())["style"]
    assert style["dash"] == "–" and style["prose_dashes"] == "normalize"
    from docx import Document
    d = Document(out)
    d.add_paragraph("Цель работы — снизить сроки.")
    d.add_paragraph("Объект исследования - процессы.")
    d.add_paragraph("Текст  с двойным пробелом.")
    d.add_heading("Заголовок с точкой.", level=2)
    bad = _TMP / "mbi_bad.docx"
    d.save(bad)
    vs = V.validate_docx(bad, style=style)
    dash_fails = [v for v in vs if v.code == "dashes" and v.tier == V.TIER_FAIL]
    assert len(dash_fails) == 2, dash_fails
    codes = {v.code for v in vs}
    assert {"double-space", "heading-period"} <= codes, codes
    itmo = V.validate_docx(bad, style={})     # ИТМО-правила: «–» тоже нельзя
    assert any(v.code == "dashes" for v in itmo)

    r = G.Report(title_page=False, profile="mbi", project_root=_TMP)
    r.table([["a", "b"], ["1", "2"]], caption="Без ссылки на неё")
    r.keywords(["одно", "два"])
    out2 = r.save(_TMP / "warn.docx", layout=False)
    codes = {v.code for v in V.validate_docx(out2)}
    assert {"table-mention", "keywords-count"} <= codes, codes


def test_continuation():
    if not (shutil.which("pdftotext") and (shutil.which("soffice") or os.environ.get(
            "GOST_REPORT_SOFFICE") or Path("/Applications/LibreOffice.app").exists())):
        print("      (skip: no LibreOffice / pdftotext)")
        return
    r = G.Report(title_page=False, profile="mbi", project_root=_TMP)
    r.text("Показатели приведены в таблице 1.")
    rows = [["№", "Показатель"]] + [[str(i), f"строка {i}"] for i in range(1, 70)]
    r.table(rows, caption="Длинная таблица", widths=[2, 14])
    r.text("Текст после таблицы.")
    out = r.save(_TMP / "long.docx")
    texts = _texts(out)
    assert "Продолжение таблицы 1" in texts, "continuation label"
    doc = _xml(out)
    assert doc.count("<w:tbl>") >= 2 and '<w:bottom w:val="nil"/>' in doc
    assert not [v for v in V.validate_docx(out) if v.tier == V.TIER_FAIL]


def main() -> int:
    global IMG
    os.environ["GOST_REPORT_CONFIG"] = str(_TMP / "absent.env")
    for key in [k for k in os.environ if k.startswith("GOST_REPORT_")
                and k != "GOST_REPORT_CONFIG"]:
        os.environ.pop(key)
    IMG = _png(_TMP / "img.png")
    check("styles: load, extends, sections, JSON, errors, dump round trip",
          test_style_loading)
    check("defaults: ITMO/GOST keep «—», template lists, no hyphenation",
          test_defaults_untouched)
    check("mbi: dashes, hyphenation, headings, tables, lists, nbsp, title, appendix",
          test_mbi_document)
    check("numbering: chapters, auto heading numbers, forward refs", test_chapter_numbering)
    check("markdown: headings, lists, table, citation, forward refs", test_markdown)
    check("title_docx: blank with split fields and logo", test_title_docx)
    check("validator: MBI dash rules and profile WARN checks", test_validator_rules)
    check("continuation: «Продолжение таблицы N» via LibreOffice", test_continuation)

    total = _PASSED + len(_FAILURES)
    if _FAILURES:
        sys.stderr.write("\n" + "\n".join(_FAILURES))
        sys.stderr.write(f"\n{len(_FAILURES)}/{total} test(s) FAILED.\n")
        return 1
    print(f"\nOK  all {total} style tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
