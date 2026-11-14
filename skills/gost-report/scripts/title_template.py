"""title_template — титульный лист из стиля, без кода в build.py.

Два способа, оба задаются в профиле (обычно в стилевом файле):

1. title_blocks — декларативный титул: список блоков сверху вниз.

    [[title.blocks]]
    lines = ["«Допущена к защите»", "Протокол № {protocol|____} от {protocol_date|________} г."]
    align = "right"            # center (по умолч.) | left | right | justify
    blank_before = 2           # пустых строк перед блоком
    size = 14                  # кегль, пт
    bold = false               # также italic, underline, caps
    indent_cm = 0              # левый отступ, см
    line_spacing = 1.0
    space_before = 0           # пт или "1line", также space_after
    anchor = "bottom"          # блок прижат к нижнему полю (город, год)
    if = "discipline"          # блок только если поле заполнено (строка или список)

   Текст: text (строка; \n — новая строка), lines (список строк) или runs
   (одна строка из кусков: [{text = "Выполнил", underline = true}, {text = ": {student_name}"}]).
   {поле} — значение TitleConfig / TitleConfig.extra / профиля, {поле|заглушка} —
   с заглушкой для пустого. Строка, ставшая пустой после подстановки, пропускается.
   Вычисляемые поля: {student_label} («Выполнил»/«Выполнили»), {students} (все ФИО,
   по строке), {work_title} (вид + номер), {teacher_line} (степень, должность, ФИО),
   {teacher_regalia} (степень, звание через запятую).

2. title_docx — готовый бланк .docx (как его выдал вуз): содержимое первой
   секции переносится как есть, с картинками (логотип) и ссылками; {поля} в
   тексте заменяются так же, как в блоках. Поля страницы берутся из бланка.
"""
from __future__ import annotations

import copy
import re
import sys
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Sequence

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

_FIELD_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?:\|([^{}]*))?\}")
_ALIGN = {
    "center": WD_ALIGN_PARAGRAPH.CENTER, "left": WD_ALIGN_PARAGRAPH.LEFT,
    "right": WD_ALIGN_PARAGRAPH.RIGHT, "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
}
_BLOCK_KEYS = {
    "text", "lines", "runs", "align", "blank_before", "blank_after", "size",
    "bold", "italic", "underline", "caps", "indent_cm", "line_spacing",
    "space_before", "space_after", "anchor", "if", "unless", "keep_empty",
}


def fill(report, template: str) -> str:
    """Подстановка {поле} / {поле|заглушка} значениями титула."""
    def repl(m: "re.Match[str]") -> str:
        value = _value(report, m.group(1))
        if value:
            return value
        return m.group(2) if m.group(2) is not None else ""
    return _FIELD_RE.sub(repl, template)


def _value(report, name: str) -> str:
    if name == "teacher_regalia":
        cfg = report.title_config
        return ", ".join(x for x in (cfg.teacher_degree, cfg.teacher_position) if x)
    return report.title_value(name)


def _as_list(value) -> List[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _check_block(block: Dict[str, Any], i: int) -> None:
    unknown = set(block) - _BLOCK_KEYS
    if unknown:
        import difflib
        hints = []
        for key in sorted(unknown):
            near = difflib.get_close_matches(key, sorted(_BLOCK_KEYS), n=1)
            hints.append(f"{key}" + (f" (может быть {near[0]}?)" if near else ""))
        raise ValueError(f"title_blocks[{i}]: неизвестные ключи: {', '.join(hints)}")
    if sum(k in block for k in ("text", "lines", "runs")) != 1:
        raise ValueError(f"title_blocks[{i}]: нужен ровно один из text / lines / runs")
    if block.get("align", "center") not in _ALIGN:
        raise ValueError(f"title_blocks[{i}]: align={block['align']!r}")


def render_blocks(report, blocks: Sequence[Dict[str, Any]]) -> None:
    """Титул из блоков профиля (см. модульный docstring)."""
    defaults = dict(getattr(report.profile, "title_block_defaults", {}) or {})
    bottom: List[str] = []
    for i, raw in enumerate(blocks):
        block = {**defaults, **raw}
        _check_block(block, i)
        if block.get("if") and not all(_value(report, f) for f in _as_list(block["if"])):
            continue
        if block.get("unless") and any(_value(report, f) for f in _as_list(block["unless"])):
            continue
        keep_empty = bool(block.get("keep_empty"))
        if "runs" in block:
            runs = [(fill(report, str(r.get("text", ""))), r) for r in block["runs"]]
            lines: List[Any] = [runs] if any(t for t, _ in runs) or keep_empty else []
        else:
            src = block.get("lines")
            if src is None:
                src = str(block["text"]).split("\n")
            lines = []
            for line in _as_list(src):
                out = fill(report, str(line))
                if out.strip() or keep_empty or not str(line).strip():
                    lines.append(out)
        if block.get("anchor") == "bottom":
            bottom.extend(x for x in lines if isinstance(x, str))
            continue
        if not lines:
            continue
        spacing = block.get("line_spacing")
        for _ in range(int(block.get("blank_before", 0))):
            _blank(report, spacing)
        for k, line in enumerate(lines):
            p = report.make_paragraph(
                align=_ALIGN[block.get("align", "center")],
                line_spacing=spacing,
                left_indent=Cm(block["indent_cm"]) if block.get("indent_cm") else None,
                space_before=(report.space(block.get("space_before")) or Pt(0)) if k == 0 else Pt(0),
                space_after=(report.space(block.get("space_after")) or Pt(0))
                if k == len(lines) - 1 else Pt(0),
            )
            parts = line if isinstance(line, list) else [(line, {})]
            for text, fmt in parts:
                if block.get("caps") or fmt.get("caps"):
                    text = text.upper()
                run = p.add_run(text)
                size = fmt.get("size", block.get("size"))
                report.set_run_font(
                    run, size=Pt(size) if size else None,
                    bold=bool(fmt.get("bold", block.get("bold", False))),
                    italic=bool(fmt.get("italic", block.get("italic", False))),
                    underline=bool(fmt.get("underline", block.get("underline", False))))
        for _ in range(int(block.get("blank_after", 0))):
            _blank(report, spacing)
    if bottom:
        report.add_bottom_block(bottom)


def _blank(report, spacing) -> None:
    report.make_paragraph(line_spacing=spacing)


# ---------------------------------------------------------------------------
# Бланк .docx
# ---------------------------------------------------------------------------

_RT_IMAGE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
_RT_LINK = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WP_DOCPR = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}docPr"


def resolve_path(report, name: str) -> Path:
    p = Path(name).expanduser()
    if p.is_absolute():
        return p
    bases = [Path(report.profile.base_dir)] if report.profile.base_dir else []
    bases += [report.paths.root, report.paths.root / ".gost-report", Path.cwd()]
    for base in bases:
        if (base / p).is_file():
            return (base / p).resolve()
    raise FileNotFoundError(
        f"title_docx {name!r} не найден (искал от: {', '.join(str(b) for b in bases)})")


def _replace_fields_in_paragraph(report, p_el) -> None:
    """{поля} в абзаце бланка: Word режет текст на run'ы произвольно, поэтому
    поле ищется по склеенному тексту и пишется в первый узел w:t."""
    nodes = [t for t in p_el.iter(qn("w:t"))]
    if not nodes:
        return
    full = "".join(t.text or "" for t in nodes)
    if "{" not in full:
        return
    owner: List[int] = []
    for idx, t in enumerate(nodes):
        owner.extend([idx] * len(t.text or ""))
    texts = [t.text or "" for t in nodes]
    starts = []
    pos = 0
    for t in texts:
        starts.append(pos)
        pos += len(t)
    for m in reversed(list(_FIELD_RE.finditer(full))):
        value = fill(report, m.group(0))
        a, b = m.start(), m.end()
        first, last = owner[a], owner[b - 1]
        for idx in range(first, last + 1):
            s0 = starts[idx]
            seg = texts[idx]
            lo, hi = max(a - s0, 0), min(b - s0, len(seg))
            texts[idx] = seg[:lo] + (value if idx == first else "") + seg[hi:]
    for t, text in zip(nodes, texts):
        t.text = text
        t.set(qn("xml:space"), "preserve")


def import_title_docx(report, name: str) -> None:
    """Переносит первую секцию бланка .docx на титул отчёта."""
    from docx import Document
    path = resolve_path(report, name)
    src = Document(str(path))
    dst_part = report.doc.part
    body = report.doc.element.body
    anchor = body.find(qn("w:sectPr"))
    rel_map: Dict[str, str] = {}
    docpr_id = 9000

    def remap(el) -> None:
        nonlocal docpr_id
        for node in el.iter():
            for attr, old in list(node.attrib.items()):
                if not attr.startswith("{" + _R_NS + "}"):
                    continue
                rel = src.part.rels.get(old)
                if rel is None:
                    del node.attrib[attr]
                    continue
                if old not in rel_map:
                    if rel.reltype == _RT_IMAGE:
                        new, _ = dst_part.get_or_add_image(BytesIO(rel.target_part.blob))
                    elif rel.reltype == _RT_LINK:
                        new = dst_part.relate_to(rel.target_ref, _RT_LINK, is_external=True)
                    else:
                        sys.stderr.write(f"gost-report warn: title_docx: связь {rel.reltype} "
                                         "не переносится, элемент без неё.\n")
                        del node.attrib[attr]
                        continue
                    rel_map[old] = new
                node.set(attr, rel_map[old])
            if node.tag == _WP_DOCPR:
                docpr_id += 1
                node.set("id", str(docpr_id))
                for a in ("descr", "title"):
                    if a in node.attrib:
                        node.attrib[a] = ""

    for el in list(src.element.body):
        if el.tag == qn("w:sectPr"):
            _copy_margins(src.sections[0], report.doc.sections[0])
            break
        clone = copy.deepcopy(el)
        inner_sect = clone.find(".//" + qn("w:sectPr"))
        if inner_sect is not None:
            inner_sect.getparent().remove(inner_sect)
        for br in clone.findall(".//" + qn("w:br")):
            if br.get(qn("w:type")) == "page":
                br.getparent().remove(br)
        for pbb in clone.findall(".//" + qn("w:pageBreakBefore")):
            pbb.getparent().remove(pbb)
        for numpr in clone.findall(".//" + qn("w:numPr")):
            numpr.getparent().remove(numpr)
        remap(clone)
        for p_el in ([clone] if clone.tag == qn("w:p") else list(clone.iter(qn("w:p")))):
            _replace_fields_in_paragraph(report, p_el)
        anchor.addprevious(clone)
        if inner_sect is not None:
            _copy_margins(src.sections[0], report.doc.sections[0])
            break


def _copy_margins(src_section, dst_section) -> None:
    for attr in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        value = getattr(src_section, attr)
        if value is not None:
            setattr(dst_section, attr, value)
