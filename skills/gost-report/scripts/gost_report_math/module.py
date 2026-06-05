"""MathModule — namespace r.math. Формулы LaTeX → нативное Word-уравнение (OMML).

Первый extracted-модуль: вся OMML-конвертация в _omml.py, раскладка формулы по
ГОСТ (центр + номер «(N)» справа) — здесь, через CoreServices ядра. Образец
структуры для остальных модулей. См. research/19 §5.
"""
from __future__ import annotations

from typing import Optional

from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.shared import Cm, Pt

from core_api import ActionableImportError
from ._omml import latex_to_omath


class _MathAPI:
    def __init__(self, core):
        self._core = core

    def formula(self, latex: str, *, where: Optional[str] = None) -> int:
        """LaTeX → OMML, центрированная формула с авто-номером «(N)» справа.
        `latex` не санируется (синтаксис); `where` проходит как обычная проза.
        Возвращает номер формулы. Раскладка идентична прежнему Report.formula."""
        core = self._core
        n = core.next_formula_number()

        omath = latex_to_omath(latex)
        printable_cm = core.printable_cm()
        sb = core.space_block

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

        if where:
            # where поддерживает инлайн-математику $...$ (напр. "$x_i$ — выборка").
            core.add_inline_paragraph(
                "где " + where,
                align=WD_ALIGN_PARAGRAPH.JUSTIFY,
                first_line_indent=Pt(0),
                left_indent=Cm(0),
                space_after=sb,
            )

        return n


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
