"""MathModule — namespace r.math. Формулы LaTeX → нативное Word-уравнение (OMML).

Первый extracted-модуль: вся OMML-конвертация в _omml.py, раскладка формулы по
ГОСТ (центр + номер «(N)» справа) — здесь, через CoreServices ядра. Образец
структуры для остальных модулей. См. research/19 §5.
"""
from __future__ import annotations


from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.shared import Cm, Pt

from core_api import ActionableImportError
from ._omml import latex_to_omath


class _MathAPI:
    def __init__(self, core):
        self._core = core

    def formula(self, latex: str, *, where=None):
        """LaTeX → OMML, центрированная формула с авто-номером «(N)» справа.
        `latex` не санируется (синтаксис); `where` проходит как обычная проза:
        строка («$a$ — слагаемое, …») или список пар [(символ, пояснение), …] —
        тогда каждый символ с новой строки, символы друг под другом.
        Интервал до и после — profile.formula_space_pt; «где» с абзацного
        отступа — profile.formula_where_indent. Возвращает номер формулы."""
        core = self._core
        n = core.next_formula_number()

        omath = latex_to_omath(latex)
        printable_cm = core.printable_cm()
        sb = getattr(core, "formula_space", core.space_block)
        prof = core.profile
        where_indent = Cm(prof.first_line_indent_cm) if getattr(
            prof, "formula_where_indent", False) else Pt(0)

        p = core.make_paragraph(
            align=WD_ALIGN_PARAGRAPH.LEFT,
            first_line_indent=Pt(0),
            left_indent=Cm(0),
            space_before=sb,
            space_after=Pt(0) if where else sb,
        )

        # Центр-таб по середине печатной области, правый таб — по правому краю.
        # Layout: TAB(center)<formula>TAB(right)(N)
        pf = p.paragraph_format
        pf.tab_stops.add_tab_stop(Cm(printable_cm / 2), WD_TAB_ALIGNMENT.CENTER)
        pf.tab_stops.add_tab_stop(Cm(printable_cm), WD_TAB_ALIGNMENT.RIGHT)

        # Tab → центр
        r_tab1 = OxmlElement("w:r")
        r_tab1.append(OxmlElement("w:tab"))
        p._p.append(r_tab1)

        # Сама формула
        p._p.append(omath)

        # ГОСТ: если за формулой идёт расшифровка «где …», формула
        # заканчивается запятой (запятая вплотную к формуле, перед номером).
        if where:
            comma_run = p.add_run(",")
            core.set_run_font(comma_run)

        # Tab → правый край → "(N)"
        num_run = p.add_run()
        num_run._element.append(OxmlElement("w:tab"))
        num_t = OxmlElement("w:t")
        num_t.text = f"({n})"
        num_run._element.append(num_t)
        core.set_run_font(num_run)

        if where and not isinstance(where, str):
            self._where_lines(list(where), sb, where_indent)
        elif where:
            # where поддерживает инлайн-математику $...$ (напр. "$x_i$ — выборка").
            core.add_inline_paragraph(
                "где " + where,
                align=WD_ALIGN_PARAGRAPH.JUSTIFY,
                first_line_indent=where_indent,
                left_indent=Cm(0),
                space_after=sb,
            )

        return n

    def _where_lines(self, pairs, sb, indent) -> None:
        """«где m – масса образца, кг;» / «V – объём образца, м³.»: первая
        строка со слова «где», следующие символы под первым символом."""
        core = self._core
        prof = core.profile
        dash = getattr(prof, "dash", "—")
        # Ширина «где » в Times New Roman ≈ 1,6 кегля.
        shift = Pt(core.body_size.pt * 1.62)
        last = len(pairs) - 1
        for i, pair in enumerate(pairs):
            if isinstance(pair, str):
                sym, desc = pair, ""
            else:
                sym, desc = pair[0], pair[1] if len(pair) > 1 else ""
            sym = sym if "$" in sym else f"${sym}$"
            desc = desc.rstrip()
            if not desc.endswith((";", ".", ",")):
                desc += "." if i == last else ";"
            # Тире между символом и пояснением ставит библиотека (как в
            # подписях): литералом, мимо санитайзера прозы.
            p = core.add_inline_paragraph(
                ("где " if i == 0 else "") + sym, align=WD_ALIGN_PARAGRAPH.JUSTIFY,
                first_line_indent=indent if i == 0 else Pt(0),
                left_indent=Cm(0) if i == 0 else indent + shift,
                space_after=sb if i == last else Pt(0))
            if desc.strip(";.,"):
                run = p.add_run(f" {dash} ")
                core.set_run_font(run)
                core.append_inline(p, desc)


class MathModule:
    namespace = "math"
    title = "Формулы (LaTeX → OMML)"
    requires_extra = None          # latex2mathml в lightweight default-тире

    def check_available(self) -> None:
        try:
            import latex2mathml  # noqa: F401
        except ImportError as e:
            raise ActionableImportError.for_extra(
                self.title, "core", "latex2mathml") from e

    def attach(self, core):
        self.check_available()
        return _MathAPI(core)

    def teardown(self) -> None:
        pass
