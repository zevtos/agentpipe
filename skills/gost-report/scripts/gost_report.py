"""
gost_report — генератор студенческих/научных работ по ГОСТ 7.32 для любого
российского вуза. Профили университетов задают поля, кегли заголовков,
название университета/факультета и прочие специфичные мелочи; чистый GOST 7.32
работает «из коробки», ИТМО — встроенный пресет.

Минимальный пример (ИТМО, дефолт):
    from gost_report import Report, TitleConfig

    r = Report(TitleConfig(
        work_type="Лабораторная работа",
        work_number="№1",
        topic="Основы работы в командной строке Unix",
        student_name="Фамилия И.О.",
        student_group="P3XXX",
        teacher_name="Фамилия И.О.",
        teacher_degree="к.т.н.",
        teacher_position="доцент",
        year="2026",
    ))
    r.toc()
    r.h1("Введение")
    r.text("Цель работы — изучить...")
    r.save("report.docx")

Любой другой вуз — передайте свой профиль:
    from gost_report import Report, TitleConfig, GOST_PROFILE, UniversityProfile

    SPBSU_PROFILE = UniversityProfile(
        university_short="«Санкт-Петербургский государственный университет»",
        faculty="Математико-механический факультет",
        toc_title="СОДЕРЖАНИЕ",
    )
    r = Report(TitleConfig(...), profile=SPBSU_PROFILE)

Зависимости: pip install python-docx
"""

from dataclasses import dataclass
from typing import Optional, Sequence

from docx import Document
from docx.document import Document as _Document
from docx.shared import Pt, Mm, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.enum.section import WD_SECTION
from docx.oxml.ns import qn
from docx.oxml import OxmlElement


# ============================================================
# Не варьируемые константы ГОСТ 7.32
# ============================================================

FONT_NAME = "Times New Roman"
FONT_SIZE_BODY = Pt(14)         # основной текст
FONT_SIZE_PAGE_NUMBER = Pt(11)  # номер страницы
FONT_SIZE_TITLE_LARGE = Pt(18)  # вид работы и тема на титульнике
LINE_SPACING_BODY = 1.5
FIRST_LINE_INDENT = Cm(1.25)    # абзацный отступ
PAGE_WIDTH = Mm(210)
PAGE_HEIGHT = Mm(297)


# ============================================================
# Профиль университета — всё, что варьируется между вузами
# ============================================================

@dataclass
class UniversityProfile:
    """Параметры оформления, специфичные для вуза.

    Дефолты соответствуют ГОСТ 7.32-2017 в наиболее распространённом
    толковании. Конкретные вузы (ИТМО, МГУ, СПбГУ и т.д.) переопределяют
    нужные поля. Создавайте свой профиль для своего вуза или принимайте
    PR-ы с новыми константами в этом модуле.
    """
    # Поля титульного листа (мм)
    title_margin_left: float = 30
    title_margin_right: float = 15
    title_margin_top: float = 20
    title_margin_bottom: float = 20
    # Поля основного текста (мм)
    body_margin_left: float = 30
    body_margin_right: float = 15
    body_margin_top: float = 20
    body_margin_bottom: float = 20
    # Кегли заголовков (пт)
    heading_size_h1: int = 16
    heading_size_h2: int = 14
    heading_size_h3: int = 14
    # Выравнивание заголовков: "center" | "left"
    heading_align_h1: str = "center"
    heading_align_h2: str = "left"
    heading_align_h3: str = "left"
    # Поведение h1
    h1_uppercase: bool = True
    h1_new_page: bool = True
    # Заголовок оглавления — ГОСТ 7.32-2017 предписывает «СОДЕРЖАНИЕ»,
    # но многие вузы (ИТМО) используют «ОГЛАВЛЕНИЕ».
    toc_title: str = "СОДЕРЖАНИЕ"
    # Поля титульника, относящиеся к вузу — fallback'и для TitleConfig
    ministry: str = "Министерство науки и высшего образования Российской Федерации"
    university_full: str = ""
    university_short: str = ""
    faculty: str = ""
    city: str = ""


# ---- Встроенные пресеты ----

GOST_PROFILE = UniversityProfile()
"""Чистый ГОСТ 7.32-2017. Поля университета не заданы — должны
прийти из TitleConfig либо из вашего собственного профиля."""

ITMO_PROFILE = UniversityProfile(
    title_margin_left=30,
    title_margin_right=20,
    title_margin_top=10,
    title_margin_bottom=10,
    body_margin_left=30,
    body_margin_right=10,
    body_margin_top=20,
    body_margin_bottom=20,
    heading_size_h1=16,
    heading_size_h2=15,
    heading_size_h3=14,
    heading_align_h1="center",
    heading_align_h2="center",
    heading_align_h3="center",
    toc_title="ОГЛАВЛЕНИЕ",
    university_full=(
        "федеральное государственное автономное "
        "образовательное учреждение высшего образования"
    ),
    university_short="«Национальный исследовательский университет ИТМО»",
    faculty="Факультет программной инженерии и компьютерной техники",
    city="Санкт-Петербург",
)
"""Университет ИТМО, ФПИиКТ. Используется как дефолт, чтобы не ломать
существующие лабораторные."""

DEFAULT_PROFILE = ITMO_PROFILE


# ============================================================
# Конфигурация титульного листа (контент, не оформление)
# ============================================================

@dataclass
class TitleConfig:
    """Содержимое титульного листа.

    Обязательные: work_type, topic, student_name, student_group, year.
    Поля университета (university_*, faculty, city, ministry) можно оставить
    пустыми — тогда они подтянутся из активного UniversityProfile.
    """
    # --- Обязательное ---
    work_type: str
    topic: str
    student_name: str
    student_group: str
    year: str

    # --- Опциональное ---
    work_number: str = ""
    variant: str = ""
    teacher_name: str = ""
    teacher_label: str = "Проверил"        # "Руководитель" для ВКР/курсовых
    teacher_degree: str = ""
    teacher_position: str = ""

    # --- Переопределение полей профиля (пусто = взять из профиля) ---
    city: str = ""
    ministry: str = ""
    university_full: str = ""
    university_short: str = ""
    faculty: str = ""


# ============================================================
# Низкоуровневые помощники работы с XML python-docx
# ============================================================

def _set_run_font(run, *, size=FONT_SIZE_BODY, bold=False, italic=False,
                  underline=False):
    run.font.name = FONT_NAME
    run.font.size = size
    run.bold = bold
    run.italic = italic
    run.underline = underline
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(attr), FONT_NAME)


def _ensure_normal_style(doc: _Document):
    normal = doc.styles["Normal"]
    normal.font.name = FONT_NAME
    normal.font.size = FONT_SIZE_BODY
    rpr = normal.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(attr), FONT_NAME)
    pf = normal.paragraph_format
    pf.line_spacing = LINE_SPACING_BODY
    pf.space_after = Pt(0)
    pf.space_before = Pt(0)


def _add_page_number_to_footer(section, *, hide_on_first=True):
    if hide_on_first:
        section.different_first_page_header_footer = True

    footer = section.footer
    footer.is_linked_to_previous = False

    for p in list(footer.paragraphs):
        p.clear()
    p = footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.line_spacing = 1.0

    run = p.add_run()
    _set_run_font(run, size=FONT_SIZE_PAGE_NUMBER)

    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.text = "PAGE"
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._element.append(fld_begin)
    run._element.append(instr)
    run._element.append(fld_end)

    if hide_on_first:
        first_footer = section.first_page_footer
        first_footer.is_linked_to_previous = False
        for p in list(first_footer.paragraphs):
            p.clear()


def _set_section_a4(section):
    section.page_width = PAGE_WIDTH
    section.page_height = PAGE_HEIGHT


def _apply_margins(section, left, right, top, bottom):
    section.left_margin = Mm(left)
    section.right_margin = Mm(right)
    section.top_margin = Mm(top)
    section.bottom_margin = Mm(bottom)


def _align_const(name: str):
    return WD_ALIGN_PARAGRAPH.CENTER if name == "center" else WD_ALIGN_PARAGRAPH.LEFT


def _configure_heading_style(doc: _Document, level: int, size_pt: int,
                             align: str):
    style = doc.styles[f"Heading {level}"]
    style.font.name = FONT_NAME
    style.font.size = Pt(size_pt)
    style.font.bold = True
    style.font.color.rgb = RGBColor(0, 0, 0)
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(attr), FONT_NAME)

    pf = style.paragraph_format
    pf.alignment = _align_const(align)
    pf.line_spacing = LINE_SPACING_BODY
    pf.space_before = Pt(0)
    pf.space_after = Pt(12)
    pf.first_line_indent = Pt(0)
    pf.keep_with_next = True


# ============================================================
# Класс отчёта — высокоуровневый API
# ============================================================

class Report:
    """Главный класс. Собирает документ из вызовов h1/text/code/figure/...

    Создание:
        r = Report(TitleConfig(...))                    # ITMO_PROFILE по умолчанию
        r = Report(TitleConfig(...), profile=GOST_PROFILE)
        r = Report(TitleConfig(...), profile=UniversityProfile(...))

    Контент: см. методы toc/h1/h2/h3/text/task/code/figure/table/numbered/bullet.
    Сохранение: r.save("report.docx").
    """

    def __init__(self, title: TitleConfig,
                 profile: UniversityProfile = DEFAULT_PROFILE):
        self._doc = Document()
        self._title = title
        self._profile = profile
        self._figure_counter = 0
        self._table_counter = 0
        self._just_broke_page = False
        self._next_num_id = 100

        # 1. Базовые стили
        _ensure_normal_style(self._doc)
        _configure_heading_style(self._doc, 1,
                                 profile.heading_size_h1,
                                 profile.heading_align_h1)
        _configure_heading_style(self._doc, 2,
                                 profile.heading_size_h2,
                                 profile.heading_align_h2)
        _configure_heading_style(self._doc, 3,
                                 profile.heading_size_h3,
                                 profile.heading_align_h3)

        # 2. Секция титульника (первая)
        section = self._doc.sections[0]
        _set_section_a4(section)
        _apply_margins(section,
                       profile.title_margin_left, profile.title_margin_right,
                       profile.title_margin_top, profile.title_margin_bottom)
        _add_page_number_to_footer(section, hide_on_first=True)
        self._build_title_page()

        # 3. Новая секция для основного текста
        body_section = self._doc.add_section(WD_SECTION.NEW_PAGE)
        _set_section_a4(body_section)
        _apply_margins(body_section,
                       profile.body_margin_left, profile.body_margin_right,
                       profile.body_margin_top, profile.body_margin_bottom)
        _add_page_number_to_footer(body_section, hide_on_first=False)
        self._just_broke_page = True

    # --------------------------------------------------------
    # Внутреннее: построение титульника
    # --------------------------------------------------------

    def _resolve(self, attr: str) -> str:
        """TitleConfig.<attr> если задано, иначе UniversityProfile.<attr>."""
        value = getattr(self._title, attr, "") or ""
        if value:
            return value
        return getattr(self._profile, attr, "") or ""

    def _add_paragraph(self, text="", *, align=WD_ALIGN_PARAGRAPH.CENTER,
                       size=FONT_SIZE_BODY, bold=False, italic=False,
                       underline=False, left_indent=None,
                       first_line_indent=None):
        p = self._doc.add_paragraph()
        p.alignment = align
        pf = p.paragraph_format
        pf.line_spacing = LINE_SPACING_BODY
        pf.space_before = Pt(0)
        pf.space_after = Pt(0)
        if left_indent is not None:
            pf.left_indent = left_indent
        if first_line_indent is not None:
            pf.first_line_indent = first_line_indent
        if text:
            run = p.add_run(text)
            _set_run_font(run, size=size, bold=bold, italic=italic,
                          underline=underline)
        return p

    def _add_runs_paragraph(self, runs, *, align=WD_ALIGN_PARAGRAPH.CENTER,
                            left_indent=None):
        p = self._doc.add_paragraph()
        p.alignment = align
        pf = p.paragraph_format
        pf.line_spacing = LINE_SPACING_BODY
        pf.space_before = Pt(0)
        pf.space_after = Pt(0)
        if left_indent is not None:
            pf.left_indent = left_indent
        for r in runs:
            run = p.add_run(r["text"])
            _set_run_font(
                run,
                size=r.get("size", FONT_SIZE_BODY),
                bold=r.get("bold", False),
                italic=r.get("italic", False),
                underline=r.get("underline", False),
            )
        return p

    def _build_title_page(self):
        cfg = self._title

        # Шапка: министерство → университет → факультет
        ministry = self._resolve("ministry")
        if ministry:
            self._add_paragraph(ministry)
        university_full = self._resolve("university_full")
        if university_full:
            self._add_paragraph(university_full)
        university_short = self._resolve("university_short")
        if university_short:
            self._add_paragraph(university_short)
        faculty = self._resolve("faculty")
        if faculty:
            self._add_paragraph()
            self._add_paragraph(faculty, italic=True)

        # Отступ к центру листа
        for _ in range(6):
            self._add_paragraph()

        # Вид работы (крупно, полужирно)
        work_type_text = cfg.work_type
        if cfg.work_number:
            work_type_text = f"{work_type_text} {cfg.work_number}".strip()
        self._add_paragraph(work_type_text, size=FONT_SIZE_TITLE_LARGE,
                            bold=True)

        # Тема (крупно, без кавычек)
        if cfg.topic:
            self._add_paragraph(cfg.topic, size=FONT_SIZE_TITLE_LARGE)

        # Вариант
        if cfg.variant:
            self._add_paragraph()
            self._add_paragraph(f"Вариант №{cfg.variant}",
                                size=FONT_SIZE_TITLE_LARGE)

        # Отступ к блоку студент/преподаватель
        for _ in range(4):
            self._add_paragraph()

        right_block_indent = Cm(9)

        self._add_runs_paragraph(
            [{"text": f"Группа: {cfg.student_group}", "underline": True}],
            align=WD_ALIGN_PARAGRAPH.LEFT,
            left_indent=right_block_indent,
        )
        self._add_runs_paragraph(
            [
                {"text": "Выполнил", "underline": True},
                {"text": f": {cfg.student_name}"},
            ],
            align=WD_ALIGN_PARAGRAPH.LEFT,
            left_indent=right_block_indent,
        )

        if cfg.teacher_name:
            self._add_paragraph(left_indent=right_block_indent,
                                align=WD_ALIGN_PARAGRAPH.LEFT)
            self._add_runs_paragraph(
                [
                    {"text": cfg.teacher_label, "underline": True},
                    {"text": ":"},
                ],
                align=WD_ALIGN_PARAGRAPH.LEFT,
                left_indent=right_block_indent,
            )
            parts = []
            if cfg.teacher_degree:
                parts.append(cfg.teacher_degree)
            if cfg.teacher_position:
                parts.append(cfg.teacher_position)
            parts.append(cfg.teacher_name)
            self._add_paragraph(
                " ".join(parts),
                align=WD_ALIGN_PARAGRAPH.LEFT,
                left_indent=right_block_indent,
            )

        # Прижимаем низ к городу/году
        for _ in range(4):
            self._add_paragraph()

        city = self._resolve("city")
        if city:
            self._add_paragraph(city)
        self._add_paragraph(cfg.year)

    # --------------------------------------------------------
    # Публичный API: контент основного текста
    # --------------------------------------------------------

    def toc(self):
        """Вставляет автоматическое оглавление (поле Word TOC). Заголовок
        — `profile.toc_title` (по умолчанию «СОДЕРЖАНИЕ» в GOST_PROFILE,
        «ОГЛАВЛЕНИЕ» в ITMO_PROFILE).

        После открытия файла в Word: ПКМ по полю → «Обновить поле».
        """
        heading = self._doc.add_paragraph()
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
        heading.paragraph_format.line_spacing = LINE_SPACING_BODY
        heading.paragraph_format.space_after = Pt(12)
        run = heading.add_run(self._profile.toc_title)
        _set_run_font(run, size=Pt(self._profile.heading_size_h1), bold=True)

        p = self._doc.add_paragraph()
        run = p.add_run()
        _set_run_font(run)

        fld_begin = OxmlElement("w:fldChar")
        fld_begin.set(qn("w:fldCharType"), "begin")
        instr = OxmlElement("w:instrText")
        instr.set(qn("xml:space"), "preserve")
        instr.text = ' TOC \\o "1-3" \\h \\z \\u '
        fld_separate = OxmlElement("w:fldChar")
        fld_separate.set(qn("w:fldCharType"), "separate")
        placeholder_run = OxmlElement("w:r")
        placeholder_text = OxmlElement("w:t")
        placeholder_text.text = ("Оглавление будет сформировано автоматически "
                                 "при обновлении поля (ПКМ → «Обновить поле»)")
        placeholder_run.append(placeholder_text)
        fld_end = OxmlElement("w:fldChar")
        fld_end.set(qn("w:fldCharType"), "end")

        run._element.append(fld_begin)
        run._element.append(instr)
        run._element.append(fld_separate)
        run._element.append(placeholder_run)
        run._element.append(fld_end)

        self.page_break()

    def h1(self, text: str):
        """Заголовок 1 уровня. Поведение управляется профилем:
        h1_new_page (новая страница), h1_uppercase (ВЕРХНИЙ регистр).
        """
        if self._profile.h1_new_page and not self._just_broke_page:
            self.page_break()
        p = self._doc.add_paragraph(style="Heading 1")
        text_to_render = text.upper() if self._profile.h1_uppercase else text
        run = p.add_run(text_to_render)
        _set_run_font(run, size=Pt(self._profile.heading_size_h1), bold=True)
        self._just_broke_page = False

    def h2(self, text: str):
        p = self._doc.add_paragraph(style="Heading 2")
        run = p.add_run(text)
        _set_run_font(run, size=Pt(self._profile.heading_size_h2), bold=True)

    def h3(self, text: str):
        p = self._doc.add_paragraph(style="Heading 3")
        run = p.add_run(text)
        _set_run_font(run, size=Pt(self._profile.heading_size_h3), bold=True)

    def text(self, text: str, *, bold=False, italic=False):
        p = self._doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        pf = p.paragraph_format
        pf.line_spacing = LINE_SPACING_BODY
        pf.first_line_indent = FIRST_LINE_INDENT
        pf.space_before = Pt(0)
        pf.space_after = Pt(0)
        run = p.add_run(text)
        _set_run_font(run, bold=bold, italic=italic)

    def task(self, text: str):
        p = self._doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        pf = p.paragraph_format
        pf.line_spacing = LINE_SPACING_BODY
        pf.first_line_indent = FIRST_LINE_INDENT
        pf.space_before = Pt(6)
        pf.space_after = Pt(6)
        run = p.add_run(text)
        _set_run_font(run, bold=True)

    def code(self, code: str):
        for line in code.split("\n"):
            p = self._doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            pf = p.paragraph_format
            pf.line_spacing = 1.0
            pf.first_line_indent = Pt(0)
            pf.left_indent = Cm(0.5)
            pf.space_before = Pt(0)
            pf.space_after = Pt(0)
            run = p.add_run(line if line else " ")
            run.font.name = "Courier New"
            run.font.size = Pt(11)
            rpr = run._element.get_or_add_rPr()
            rfonts = rpr.find(qn("w:rFonts"))
            if rfonts is None:
                rfonts = OxmlElement("w:rFonts")
                rpr.append(rfonts)
            for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
                rfonts.set(qn(attr), "Courier New")

    def figure(self, image_path: str, caption: str, *,
               width_cm: Optional[float] = None):
        self._figure_counter += 1
        p = self._doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        pf = p.paragraph_format
        pf.line_spacing = LINE_SPACING_BODY
        pf.first_line_indent = Pt(0)
        pf.space_before = Pt(6)
        pf.space_after = Pt(0)
        run = p.add_run()
        if width_cm is not None:
            run.add_picture(image_path, width=Cm(width_cm))
        else:
            run.add_picture(image_path)

        cap = self._doc.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cpf = cap.paragraph_format
        cpf.line_spacing = LINE_SPACING_BODY
        cpf.first_line_indent = Pt(0)
        cpf.space_before = Pt(0)
        cpf.space_after = Pt(6)
        cap_run = cap.add_run(f"Рисунок {self._figure_counter} — {caption}")
        _set_run_font(cap_run)

    def table(self, rows: Sequence[Sequence[str]], caption: str = "",
              *, has_header: bool = True):
        if not rows:
            return
        self._table_counter += 1

        if caption:
            cap = self._doc.add_paragraph()
            cap.alignment = WD_ALIGN_PARAGRAPH.LEFT
            cpf = cap.paragraph_format
            cpf.line_spacing = LINE_SPACING_BODY
            cpf.first_line_indent = Pt(0)
            cpf.space_before = Pt(6)
            cpf.space_after = Pt(0)
            cap_run = cap.add_run(
                f"Таблица {self._table_counter} — {caption}")
            _set_run_font(cap_run)

        n_cols = max(len(r) for r in rows)
        table = self._doc.add_table(rows=len(rows), cols=n_cols)
        table.style = "Table Grid"
        for i, row_data in enumerate(rows):
            for j in range(n_cols):
                cell = table.rows[i].cells[j]
                cell.text = ""
                p = cell.paragraphs[0]
                p.paragraph_format.line_spacing = 1.15
                p.paragraph_format.first_line_indent = Pt(0)
                value = row_data[j] if j < len(row_data) else ""
                run = p.add_run(value)
                _set_run_font(run, bold=(has_header and i == 0))

    def _create_independent_num(self, abstract_num_id: str) -> int:
        numbering = self._doc.part.numbering_part.element
        num_id = self._next_num_id
        self._next_num_id += 1

        num = OxmlElement("w:num")
        num.set(qn("w:numId"), str(num_id))
        abs_ref = OxmlElement("w:abstractNumId")
        abs_ref.set(qn("w:val"), abstract_num_id)
        num.append(abs_ref)
        override = OxmlElement("w:lvlOverride")
        override.set(qn("w:ilvl"), "0")
        start_override = OxmlElement("w:startOverride")
        start_override.set(qn("w:val"), "1")
        override.append(start_override)
        num.append(override)

        numbering.append(num)
        return num_id

    def _find_abstract_num_for_style(self, style_name: str) -> Optional[str]:
        try:
            style = self._doc.styles[style_name]
        except KeyError:
            return None
        style_pPr = style.element.find(qn("w:pPr"))
        if style_pPr is None:
            return None
        numPr = style_pPr.find(qn("w:numPr"))
        if numPr is None:
            return None
        numId_el = numPr.find(qn("w:numId"))
        if numId_el is None:
            return None
        style_num_id = numId_el.get(qn("w:val"))

        numbering = self._doc.part.numbering_part.element
        for num in numbering.findall(qn("w:num")):
            if num.get(qn("w:numId")) == style_num_id:
                abs_ref = num.find(qn("w:abstractNumId"))
                if abs_ref is not None:
                    return abs_ref.get(qn("w:val"))
        return None

    def _add_list_paragraph(self, text: str, num_id: int):
        p = self._doc.add_paragraph()
        pf = p.paragraph_format
        pf.line_spacing = LINE_SPACING_BODY
        pf.space_after = Pt(0)

        pPr = p._p.get_or_add_pPr()
        numPr = OxmlElement("w:numPr")
        ilvl = OxmlElement("w:ilvl")
        ilvl.set(qn("w:val"), "0")
        numId_el = OxmlElement("w:numId")
        numId_el.set(qn("w:val"), str(num_id))
        numPr.append(ilvl)
        numPr.append(numId_el)
        pPr.append(numPr)

        run = p.add_run(text)
        _set_run_font(run)

    def numbered(self, items):
        if isinstance(items, str):
            items = [items]
        if not items:
            return
        try:
            _ = self._doc.part.numbering_part
        except (AttributeError, KeyError):
            tmp = self._doc.add_paragraph(style="List Number")
            tmp._element.getparent().remove(tmp._element)
        abstract_id = self._find_abstract_num_for_style("List Number")
        if abstract_id is None:
            for item in items:
                p = self._doc.add_paragraph(style="List Number")
                pf = p.paragraph_format
                pf.line_spacing = LINE_SPACING_BODY
                pf.space_after = Pt(0)
                run = p.add_run(item)
                _set_run_font(run)
            return
        num_id = self._create_independent_num(abstract_id)
        for item in items:
            self._add_list_paragraph(item, num_id)

    def bullet(self, items):
        if isinstance(items, str):
            items = [items]
        if not items:
            return
        try:
            _ = self._doc.part.numbering_part
        except (AttributeError, KeyError):
            tmp = self._doc.add_paragraph(style="List Bullet")
            tmp._element.getparent().remove(tmp._element)
        abstract_id = self._find_abstract_num_for_style("List Bullet")
        if abstract_id is None:
            for item in items:
                p = self._doc.add_paragraph(style="List Bullet")
                pf = p.paragraph_format
                pf.line_spacing = LINE_SPACING_BODY
                pf.space_after = Pt(0)
                run = p.add_run(item)
                _set_run_font(run)
            return
        num_id = self._create_independent_num(abstract_id)
        for item in items:
            self._add_list_paragraph(item, num_id)

    def page_break(self):
        p = self._doc.add_paragraph()
        run = p.add_run()
        run.add_break(WD_BREAK.PAGE)
        self._just_broke_page = True

    def save(self, path: str):
        self._doc.save(path)
        return path

    @property
    def doc(self) -> _Document:
        return self._doc

    @property
    def profile(self) -> UniversityProfile:
        return self._profile
