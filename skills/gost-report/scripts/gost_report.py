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

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union
import zipfile

from docx import Document
from docx.document import Document as _Document
from docx.shared import Pt, Mm, Cm, Length, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.enum.section import WD_SECTION
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

from _paths import paths, _caller_file, _find_root
from typography import (NBHY, glue_short_words, normalize_dashes, required_nbsp,
                        split_emphasis, typograf)


# ============================================================
# Не варьируемые константы ГОСТ 7.32
# ============================================================

FONT_NAME = "Times New Roman"
FONT_SIZE_BODY = Pt(14)         # основной текст
FONT_SIZE_PAGE_NUMBER = Pt(11)  # номер страницы
FONT_SIZE_TITLE_LARGE = Pt(18)  # вид работы и тема на титульнике
LINE_SPACING_BODY = 1.5
FIRST_LINE_INDENT = Cm(1.25)    # абзацный отступ
PAGE_WIDTH_MM = 210
PAGE_HEIGHT_MM = 297
PAGE_WIDTH = Mm(PAGE_WIDTH_MM)
PAGE_HEIGHT = Mm(PAGE_HEIGHT_MM)

# Line-spacing presets для тел разных типов абзацев.
LINE_SPACING_CODE = 1.0
LINE_SPACING_TABLE = 1.15

# Вертикальный отступ между блоками (figure caption, formula, list).
SPACE_BLOCK = Pt(6)

# Титульник: пустые абзацы-разделители и сдвиг правого блока.
_TITLE_TOP_GAP = 6                    # пустых абзацев от шапки до вида работы
_TITLE_MID_GAP = 4                    # пустых абзацев перед блоком студент/преподаватель
_RIGHT_BLOCK_REST_INDENT = Cm(2.2)    # доп. сдвиг continuation-имён (≈ ширина "Выполнили: ")


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

    # --- Типографика основного текста (дефолты = ГОСТ 7.32) ---
    # Школы и некоторые вузы требуют другое: TNR 12, отступ 1 см и т.п.
    body_font_size: float = 14             # пт, весь текст кроме заголовков
    line_spacing: float = 1.5              # множитель, основной текст и заголовки
    first_line_indent_cm: float = 1.25     # абзацный отступ («красная строка»)
    heading_space_after_pt: float = 12     # интервал после заголовков и «ОГЛАВЛЕНИЯ»
    block_space_pt: float = 6              # интервал вокруг рисунков/таблиц/формул
    # Номер страницы: "<bottom|top>-<left|center|right>"
    page_number_position: str = "bottom-center"
    page_number_size: float = 11           # пт
    # --- Титульник ---
    title_size_large: float = 18           # пт, вид работы и тема
    title_right_block_indent_cm: float = 9  # сдвиг блока «Выполнил/Проверил» от левого поля
    # "group_line"  — строка «Группа: …», ниже «Выполнил: ФИО» (вуз)
    # "label_group" — «Выполнила: ученица 9 «Б» класса», ФИО строками ниже
    #                 под меткой, как у руководителя (школа)
    title_student_layout: str = "group_line"
    title_group_template: str = "Группа: {group}"   # {group} ← TitleConfig.student_group
    # Полностью свой титульник: callable(report) вместо встроенной раскладки.
    # Внутри — report.add_text_paragraph(...), report.add_runs_paragraph(...),
    # report.add_bottom_block([...]). Секция, поля и скрытый номер уже настроены.
    title_builder: Optional[Callable[["Report"], None]] = None

    # ================================================================
    # Поля ниже добавлены для произвольных положений о ВКР (МБИ и др.).
    # Дефолты повторяют прежнее поведение байт-в-байт: профиль ИТМО/ГОСТ,
    # собранный без них, даёт тот же .docx. Описание каждого поля:
    # `python3 scripts/styles.py fields`, справочник: references/profiles.md.
    # ================================================================
    # --- Служебное (заполняет загрузчик стилей) ---
    name: str = ""                         # имя профиля: sentinel, сообщения
    base_dir: str = ""                     # каталог файла стиля: title_docx и пути
    # --- Шрифт ---
    font_name: str = FONT_NAME
    # --- Проза: тире, неразрывные пробелы, переносы, разметка ---
    dash: str = "—"                        # тире в «Рисунок 1 — …», «Примечание — …»
    # "strip" — тире из прозы убираются (правило №1 скилла);
    # "normalize" — « - », « — » между словами → « <dash> », диапазоны «45–60»
    #   и «–» сохраняются; "keep" — проза не трогается (как allow_dashes=True)
    prose_dashes: str = "strip"
    nbsp: str = "off"                      # "off" | "required" | "full"
    auto_hyphenation: bool = False         # автоперенос без режима typography
    widow_control: bool = False            # запрет висячих строк в тексте
    language: str = ""                     # w:lang документа; "" = ru-RU при переносах
    markup: bool = False                   # **жирный** и *курсив* в прозе
    # --- Заголовки ---
    heading_indent_cm_h1: float = 0        # абзацный отступ первой строки
    heading_indent_cm_h2: float = 0
    heading_indent_cm_h3: float = 0
    # Интервалы: число = пт, строка с единицей: "1line", "12pt", "0.5cm".
    # None = прежнее поведение (0 до, heading_space_after_pt после).
    heading_space_before_h1: Optional[Union[float, str]] = None
    heading_space_before_h2: Optional[Union[float, str]] = None
    heading_space_before_h3: Optional[Union[float, str]] = None
    heading_space_after_h1: Optional[Union[float, str]] = None
    heading_space_after_h2: Optional[Union[float, str]] = None
    heading_space_after_h3: Optional[Union[float, str]] = None
    heading_bold_h1: bool = True
    heading_bold_h2: bool = True
    heading_bold_h3: bool = True
    heading_italic_h1: bool = False
    heading_italic_h2: bool = False
    heading_italic_h3: bool = False
    heading_line_spacing: Optional[float] = None   # None = line_spacing
    heading_glue: bool = False             # предлоги/союзы не в конце строки заголовка
    # Автонумерация «1», «1.1», «1.1.1» (без точки в конце). Заголовок, текст
    # которого уже начинается с номера, и структурные элементы из
    # unnumbered_headings не нумеруются; r.h1(..., numbered=False) — вручную.
    heading_numbering: bool = False
    heading_number_sep: str = " "          # между номером и текстом
    unnumbered_headings: Tuple[str, ...] = (
        "ВВЕДЕНИЕ", "ЗАКЛЮЧЕНИЕ", "РЕФЕРАТ", "АННОТАЦИЯ", "СОДЕРЖАНИЕ",
        "ОГЛАВЛЕНИЕ", "СПИСОК ИСПОЛЬЗОВАННЫХ ИСТОЧНИКОВ", "СПИСОК ЛИТЕРАТУРЫ",
        "СПИСОК ИСПОЛЬЗОВАННОЙ ЛИТЕРАТУРЫ", "БИБЛИОГРАФИЧЕСКИЙ СПИСОК",
        "ТЕРМИНЫ И ОПРЕДЕЛЕНИЯ", "ОПРЕДЕЛЕНИЯ, ОБОЗНАЧЕНИЯ И СОКРАЩЕНИЯ",
        "ПЕРЕЧЕНЬ СОКРАЩЕНИЙ И ОБОЗНАЧЕНИЙ", "ПЕРЕЧЕНЬ СОКРАЩЕНИЙ",
        "СПИСОК СОКРАЩЕНИЙ", "СПИСОК ИСПОЛНИТЕЛЕЙ", "ВЫВОДЫ ПО ГЛАВЕ*",
        "ВЫВОДЫ ПО РАЗДЕЛУ*", "ПРИЛОЖЕНИЕ*",
    )                                      # «*» в конце — совпадение по началу
    # --- Подписи рисунков и таблиц ---
    figure_label: str = "Рисунок"
    table_label: str = "Таблица"
    figure_caption_align: str = "center"
    table_caption_align: str = "left"
    table_caption_indent_cm: float = 0
    caption_font_size: Optional[float] = None      # None = кегль текста
    caption_line_spacing: Optional[float] = None   # None = line_spacing
    caption_hyphenation: bool = True       # False: подписи без переносов
    caption_glue: bool = False             # предлоги/союзы не в конце строки подписи
    caption_keep_with_next: bool = False   # подпись таблицы не отрывается от таблицы,
                                           # рисунок от своей подписи
    # "global" — сквозная («Рисунок 3»), "chapter" — по главам («Рисунок 1.3»).
    # В приложении всегда «Рисунок А.1».
    numbering: str = "global"
    formula_numbering: str = ""            # "" = как numbering
    # --- Интервалы блоков ---
    formula_space_pt: Optional[float] = None   # None = block_space_pt
    # --- Таблицы ---
    table_font_size: Optional[float] = None    # None = кегль текста
    table_line_spacing: float = 1.15
    table_header_bold: bool = True
    table_header_align: str = ""           # "" = как столбец; "center" и т. п.
    table_repeat_header: bool = False      # шапка повторяется на каждой странице
    table_cant_split: bool = False         # строка не рвётся между страницами
    table_keep_together: bool = False      # таблица целиком на одной странице
    # Таблица длиннее страницы: r.save() находит разрыв вёрсткой LibreOffice,
    # режет таблицу и ставит над второй частью «Продолжение таблицы N».
    table_continuation: bool = False
    table_continuation_text: str = "Продолжение таблицы {n}"
    # --- Перечисления ---
    # "hanging" — как раньше (висячий отступ, табуляция после маркера);
    # "paragraph" — маркер на абзацном отступе, строки от левого поля,
    #   после маркера пробел, выравнивание по ширине (ГОСТ 2.105, 7.32).
    list_layout: str = "hanging"
    bullet_char: str = ""                  # "" = «•» шаблона; "–" по ГОСТ
    numbered_format: str = ""              # "" = «1.»; "%1)" → «1)»
    lettered_format: str = "{letter})"
    list_letters: str = "абвгдежиклмнпрстуфхцшщэюя"  # без ё з й о ч ъ ы ь
    list_nested_indent_cm: float = 0       # доп. отступ вложенного уровня
    # --- Формулы ---
    formula_where_indent: bool = False     # «где …» с абзацного отступа
    # --- Примечания и примеры ---
    note_label: str = "Примечание"
    notes_label: str = "Примечания"
    example_label: str = "Пример"
    examples_label: str = "Примеры"
    note_font_size: Optional[float] = None
    # --- Реферат ---
    keywords_min: int = 5
    keywords_max: int = 15
    keywords_align: str = "left"
    volume_template: str = (
        "Выпускная квалификационная работа содержит {pages} с., {figures} рис., "
        "{tables} табл., {sources} {sources_word}, {appendices} прил.")
    # --- Подзаголовок r.subtitle ---
    subtitle_italic: bool = True
    subtitle_bold: bool = False
    subtitle_line_spacing: float = 1.0
    subtitle_space_after: Union[float, str] = 0
    subtitle_attach: bool = False          # интервал после заголовка уходит под подзаголовок
    # --- Код ---
    code_font_name: str = "Courier New"
    code_font_size: float = 11
    code_indent_cm: float = 0.5
    # --- Список источников ---
    bib_style: str = "7.1-2003"            # | "7.0.100-2018"
    bib_title: str = "Список использованных источников"
    bib_align: str = "left"
    bib_indent_cm: float = 0
    bib_min_sources: int = 0               # меньше — WARN валидатора
    # --- Приложения ---
    appendix_label: str = "ПРИЛОЖЕНИЕ"
    appendix_letters: str = "АБВГДЕЖИКЛМНПРСТУФХЦШЩЭЮЯ"
    appendix_font_size: Optional[float] = None
    # --- Титул по шаблону (вместо встроенной раскладки) ---
    # title_blocks — декларативный титул: список блоков с текстом и {полями}
    # TitleConfig (references/profiles.md, «Титул из стиля»). title_docx —
    # готовый бланк .docx с {полями}: переносится как есть, вместе с картинками.
    title_blocks: Tuple[Dict[str, Any], ...] = ()
    title_block_defaults: Dict[str, Any] = field(default_factory=dict)  # общие ключи блоков
    title_docx: str = ""
    title_required: Tuple[str, ...] = ()   # обязательные поля шаблонного титула
    # --- Проверки валидатора сверх базовых (WARN) ---
    # "double-space", "heading-period", "paragraph-spacing", "table-mention",
    # "figure-mention", "keywords-count", "bib-count"; ("all",) — все.
    checks: Tuple[str, ...] = ()


# ---- Встроенные пресеты ----

GOST_PROFILE = UniversityProfile(name="gost")
"""Чистый ГОСТ 7.32-2017. Поля университета не заданы — должны
прийти из TitleConfig либо из вашего собственного профиля."""

ITMO_PROFILE = UniversityProfile(
    name="itmo",
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


def load_profile(spec: Union[str, Path, "UniversityProfile", Dict[str, Any]],
                 **overrides: Any) -> "UniversityProfile":
    """Профиль из стилевого файла (.toml/.json), имени встроенного стиля
    («gost», «itmo», «mbi», …), словаря или готового профиля, с наложением
    overrides. Стиль может наследоваться: `extends = "gost"`. Подробно —
    references/profiles.md; список полей — `python3 scripts/styles.py fields`."""
    import styles
    return styles.load_profile(spec, **overrides)


def __getattr__(name: str):
    """Пресеты из стилевых файлов (MBI_PROFILE и др.) грузятся при первом
    обращении: импорт модуля не читает файлы и не требует tomllib."""
    if name.endswith("_PROFILE"):
        import styles
        stem = name[: -len("_PROFILE")].lower().replace("_", "-")
        try:
            profile = styles.load_profile(stem)
        except LookupError:
            raise AttributeError(name) from None
        globals()[name] = profile
        return profile
    raise AttributeError(name)


# Sentinel for "do not set this attribute — inherit from the paragraph style".
# Used by _make_paragraph callers (notably list paragraphs, which must keep
# w:before unset so the value comes from the List Number / List Bullet style).
_INHERIT = object()


# ============================================================
# Стилизованный run внутри одного абзаца
# ============================================================

@dataclass(frozen=True)
class StyledRun:
    """Кусочек текста с собственным форматированием, для абзацев из
    нескольких run'ов (например "Группа: P3XXX" где "Группа:" подчёркнут
    а номер — обычный). Используется на титульнике.
    """
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    # docx.shared.Length (либо None → кегль основного текста профиля). Pt/Cm/Mm возвращают
    # Length-объекты; используем Optional[Length] вместо object для ясности.
    size: Optional["Length"] = None


# ============================================================
# Ошибка валидации
# ============================================================

class GostValidationError(RuntimeError):
    """Поднимается из Report.save() когда сгенерированный .docx не проходит
    автоматическую проверку ГОСТ.

    Текст исключения — единственная prose-инструкция про валидацию во всём
    скилле. Она появляется в shell-выводе только когда модель уже накосячила.
    Поэтому фразировка имеет значение: явно сказано «перегенерируй», «не
    подавляй», «не используй python-docx напрямую» — это именно те ошибочные
    реакции, которые надо превентить.
    """


# ============================================================
# Конфигурация титульного листа (контент, не оформление)
# ============================================================

@dataclass
class TitleConfig:
    """Содержимое титульного листа.

    Обязательные по смыслу: work_type, topic, student_name (или student_names),
    student_group, year. Любое из них можно опустить здесь и задать через
    env-переменные (`~/.config/gost-report/config` или `os.environ`), см.
    `_load_gost_env`. Env побеждает build.py, если задан и не пуст.

    Поля университета (university_*, faculty, city, ministry) можно оставить
    пустыми — тогда они подтянутся из активного UniversityProfile.
    """
    # --- Обязательное по смыслу (env может заполнить) ---
    work_type: str = ""
    topic: str = ""
    student_name: str = ""
    student_group: str = ""
    year: str = ""

    # --- Команда (если задан student_names — рендерится список) ---
    student_names: List[str] = field(default_factory=list)
    student_label: str = ""                # пусто = авто: "Выполнили"/"Выполнил"

    # --- Опциональное ---
    work_number: str = ""
    discipline: str = ""                   # «Системное администрирование» — строка
                                           # «по дисциплине «…»» на титуле
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

    # --- ВКР и курсовые: поля для титула по шаблону (title_blocks) ---
    department: str = ""                   # кафедра
    direction: str = ""                    # направление подготовки «38.03.05 Бизнес-информатика»
    program: str = ""                      # профиль / программа
    course: str = ""                       # курс
    study_form: str = ""                   # форма обучения: «очной»
    head_label: str = ""                   # «И. о. заведующего кафедрой»
    head_name: str = ""
    head_degree: str = ""                  # «канд. экон. наук, доцент»
    # Любые другие {поля} шаблона титула. Env: GOST_REPORT_EXTRA_<ИМЯ>.
    extra: Dict[str, str] = field(default_factory=dict)


# ============================================================
# Env-конфиг: ~/.config/gost-report/config + os.environ
# ============================================================

# Ключи env, которые накладываются на TitleConfig. Значение — имя атрибута
# TitleConfig. Префикс "GOST_REPORT_" + UPPER(имя атрибута) для каждого.
_ENV_PREFIX = "GOST_REPORT_"
_ENV_STR_FIELDS = (
    "work_type", "topic", "student_name", "student_group", "year",
    "student_label",
    "work_number", "discipline", "variant",
    "teacher_name", "teacher_label", "teacher_degree", "teacher_position",
    "city", "ministry", "university_full", "university_short", "faculty",
    "department", "direction", "program", "course", "study_form",
    "head_label", "head_name", "head_degree",
)
_ENV_EXTRA_PREFIX = f"{_ENV_PREFIX}EXTRA_"


def _config_file_path() -> Path:
    """Стандартный путь конфига: $XDG_CONFIG_HOME/gost-report/config
    либо ~/.config/gost-report/config. Можно переопределить через
    GOST_REPORT_CONFIG."""
    override = os.environ.get(f"{_ENV_PREFIX}CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if base:
        return Path(base).expanduser() / "gost-report" / "config"
    return Path.home() / ".config" / "gost-report" / "config"


_CONFIG_LINE_RE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$')


def _parse_config_file(path: Path) -> Dict[str, str]:
    """Простой парсер .env-формата: KEY=VALUE построчно. Поддерживает
    `export KEY=...`, кавычки (' или "), комментарии (#) и пустые строки.
    Никаких подстановок переменных или escape-последовательностей."""
    if not path.is_file():
        return {}
    out: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = _CONFIG_LINE_RE.match(raw_line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        out[key] = value
    return out


_SUBJECT_CONFIG_NAME = ".gost-report.env"


def _find_subject_config() -> Optional[Path]:
    """Subject-уровневый конфиг: обход вверх от build.py до project root,
    ищем `.gost-report.env`. Возвращает ближайший. Покрывает и
    отдельные репы по предмету, и монорепо с несколькими предметами:
    конфиг в папке `физика/` побеждает конфиг в корне монорепо."""
    start = _caller_file() or Path.cwd()
    if start.is_file():
        start = start.parent
    start = start.resolve()
    root = _find_root(start)
    upper = root.resolve() if root else start

    cur = start
    while True:
        candidate = cur / _SUBJECT_CONFIG_NAME
        if candidate.is_file():
            return candidate
        if cur == upper or cur.parent == cur:
            break
        cur = cur.parent
    return None


def _load_gost_env() -> Dict[str, str]:
    """Сливает три слоя env: global config → subject config → os.environ.
    Каждый последующий перекрывает предыдущий. Пустые/отсутствующие
    значения не затирают то, что выставлено выше по приоритету.

    Работа за другого человека (заказ, сестра): в subject-конфиге
    `GOST_REPORT_IGNORE_GLOBAL_CONFIG=1` отключает глобальный слой, чтобы
    личные ФИО/группа владельца не протекли на чужой титул."""
    subject = _find_subject_config()
    subject_env = _parse_config_file(subject) if subject is not None else {}
    ignore_global = subject_env.get(
        f"{_ENV_PREFIX}IGNORE_GLOBAL_CONFIG", "").strip().lower() in _TRUTHY_ENV
    merged = {} if ignore_global else _parse_config_file(_config_file_path())
    merged.update(subject_env)
    for key, val in os.environ.items():
        if key.startswith(_ENV_PREFIX) and val:
            merged[key] = val
    return merged


# Маркер заглушек из install-template config.env.example. Значение, содержащее
# этот токен, трактуется как НЕзаполненное везде — чтобы первый отчёт не ушёл с
# «ЗАПОЛНИ ФИО» на титуле, а валидатор указал, где править.
_PLACEHOLDER_TOKEN = "ЗАПОЛНИ"


def _is_placeholder(value: str) -> bool:
    return _PLACEHOLDER_TOKEN in (value or "").upper()


def _apply_env_overrides(cfg: TitleConfig) -> TitleConfig:
    """Накладывает env поверх TitleConfig. Env побеждает build.py, если
    непустое и не заглушка. Пустое/отсутствующее/заглушка-env — оставляет
    значение из build.py.

    Особый случай: GOST_REPORT_STUDENT_NAMES — список через запятую,
    перекрывает только cfg.student_names (не student_name).
    """
    env = _load_gost_env()
    for field_name in _ENV_STR_FIELDS:
        env_key = f"{_ENV_PREFIX}{field_name.upper()}"
        env_val = env.get(env_key, "")
        if env_val and not _is_placeholder(env_val):
            setattr(cfg, field_name, env_val)
    names_raw = env.get(f"{_ENV_PREFIX}STUDENT_NAMES", "")
    if names_raw and not _is_placeholder(names_raw):
        cfg.student_names = [n.strip() for n in names_raw.split(",") if n.strip()]
    for key, val in env.items():
        if key.startswith(_ENV_EXTRA_PREFIX) and val and not _is_placeholder(val):
            cfg.extra = {**cfg.extra, key[len(_ENV_EXTRA_PREFIX):].lower(): val}
    return cfg


_TRUTHY_ENV = {"1", "true", "yes", "on", "да"}


def _env_flag(name: str) -> bool:
    """Булев env-флаг из слитого gost-env (config-файлы + os.environ).
    Префикс GOST_REPORT_ добавляется автоматически. Truthy: 1/true/yes/on/да."""
    return _load_gost_env().get(
        f"{_ENV_PREFIX}{name}", "").strip().lower() in _TRUTHY_ENV


def _resolve_student_label(cfg: TitleConfig) -> str:
    """Авто-выбор: команда — "Выполнили", иначе — "Выполнил".
    Явно заданное cfg.student_label всегда побеждает."""
    if cfg.student_label:
        return cfg.student_label
    if cfg.student_names:
        return "Выполнили"
    return "Выполнил"


# ============================================================
# Низкоуровневые помощники работы с XML python-docx
# ============================================================

_RFONTS_ATTRS = ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia")
_RFONTS_THEME_ATTRS = ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme")


def _force_run_fonts(rpr, font_name: str) -> None:
    """Принудительно ставит ``font_name`` во все <w:rFonts> слоты (ascii,
    hAnsi, cs, eastAsia). Используется в стилях и run-level форматировании
    чтобы Word гарантированно отрисовал нужный шрифт для всех кодовых
    диапазонов (латиница, кириллица, восточно-азиатские, complex)."""
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for attr in _RFONTS_ATTRS:
        rfonts.set(qn(attr), font_name)
    # Theme-атрибуты в шаблоне python-docx побеждают явный w:ascii: без этого
    # часть заголовка (например, пробелы) уезжает в Calibri.
    for attr in _RFONTS_THEME_ATTRS:
        rfonts.attrib.pop(qn(attr), None)


def _set_run_font(run, *, size=FONT_SIZE_BODY, bold=False, italic=False,
                  underline=False, font=FONT_NAME):
    run.font.name = font
    run.font.size = size
    run.bold = bold
    run.italic = italic
    run.underline = underline
    _force_run_fonts(run._element.get_or_add_rPr(), font)


_THUMBNAIL_PART_NAME = "docProps/thumbnail.jpeg"
_THUMBNAIL_REL_RE = re.compile(
    rb'<Relationship[^>]*Target="docProps/thumbnail\.jpeg"[^>]*/>'
)


# Фиксированная дата для детерминированного docProps/core.xml. python-docx
# проставляет dcterms:created/modified текущим временем при save(), из-за чего
# один и тот же документ, собранный в двух процессах, даёт разные байты core.xml
# (остальные part-ы уже детерминированы). Пиним на константу; уважаем
# SOURCE_DATE_EPOCH (конвенция reproducible-builds), если задан.
_FALLBACK_TIMESTAMP = datetime(2000, 1, 1, 0, 0, 0)  # naive UTC


def _deterministic_timestamp() -> datetime:
    """Naive-UTC datetime для core.xml: SOURCE_DATE_EPOCH если задан, иначе константа."""
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw:
        try:
            return datetime.fromtimestamp(
                int(raw), tz=timezone.utc
            ).replace(tzinfo=None)
        except (ValueError, OverflowError, OSError):
            pass
    return _FALLBACK_TIMESTAMP


def _strip_python_docx_fingerprints(doc: _Document) -> None:
    """Стирает дефолтные строки шаблона python-docx из core_properties:
    «python-docx» в dc:creator, «generated by python-docx» в dc:description,
    фоссилизированный таймстемп 2013-12-23 в dcterms:created/modified.
    Без этого любой, кто откроет «Свойства документа» в Word или распакует
    .docx как zip, сразу видит, что файл сгенерирован библиотекой.

    Таймстемпы пинятся на фиксированную дату (см. ``_deterministic_timestamp``),
    а cp:revision/lastModifiedBy нормализуются, чтобы core.xml был байт-в-байт
    воспроизводимым между процессами.
    """
    cp = doc.core_properties
    cp.author = ""
    cp.comments = ""
    cp.last_modified_by = ""
    cp.revision = 1
    pinned = _deterministic_timestamp()
    cp.created = pinned
    cp.modified = pinned


def _strip_thumbnail_part(docx_path: Path) -> None:
    """Удаляет дефолтный thumbnail python-docx из сохранённого .docx.

    Эта jpeg-картинка (8.3 КБ, 395x512) одинакова в каждом python-docx файле,
    однозначно идентифицирует библиотеку и не появляется в обычных Word-файлах,
    сохранённых без галки «Save Thumbnail». Удаляем сам part и Relationship
    на него в `_rels/.rels`; запись `<Default Extension="jpeg"/>` в
    `[Content_Types].xml` оставляем — без привязанного файла она безвредна
    и встречается в Word-файлах с jpeg-картинками внутри (figure-ы).
    """
    try:
        with zipfile.ZipFile(docx_path, "r") as zin:
            names = set(zin.namelist())
            if _THUMBNAIL_PART_NAME not in names:
                return
            entries = [
                (item, zin.read(item.filename))
                for item in zin.infolist()
                if item.filename != _THUMBNAIL_PART_NAME
            ]
    except (zipfile.BadZipFile, OSError):
        return
    tmp = docx_path.with_suffix(docx_path.suffix + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item, data in entries:
                if item.filename == "_rels/.rels":
                    data = _THUMBNAIL_REL_RE.sub(b"", data)
                zout.writestr(item, data)
        tmp.replace(docx_path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _ensure_normal_style(doc: _Document, *, size=FONT_SIZE_BODY,
                         line_spacing=LINE_SPACING_BODY, font=FONT_NAME):
    normal = doc.styles["Normal"]
    normal.font.name = font
    normal.font.size = size
    _force_run_fonts(normal.element.get_or_add_rPr(), font)
    pf = normal.paragraph_format
    pf.line_spacing = line_spacing
    pf.space_after = Pt(0)
    pf.space_before = Pt(0)


def _append_word_field(run, instr_text: str, *,
                       placeholder: Optional[str] = None,
                       preserve_space: bool = False) -> None:
    """Вставляет Word-поле в ``run``: ``<w:fldChar begin>`` + ``<w:instrText>``
    + (опционально) ``<w:fldChar separate>`` + placeholder-run + ``<w:fldChar end>``.

    Используется для PAGE (footer) и TOC (заголовок оглавления). Если задан
    ``placeholder`` — добавляется separate-маркер и placeholder-run
    с текстом, который Word/Pages/LibreOffice заменят при обновлении полей.
    ``preserve_space=True`` ставит ``xml:space="preserve"`` на ``instrText``
    (нужно для TOC, где переключатели разделены пробелами).
    """
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    if preserve_space:
        instr.set(qn("xml:space"), "preserve")
    instr.text = instr_text

    run._element.append(fld_begin)
    run._element.append(instr)

    if placeholder is not None:
        fld_separate = OxmlElement("w:fldChar")
        fld_separate.set(qn("w:fldCharType"), "separate")
        placeholder_run = OxmlElement("w:r")
        placeholder_text = OxmlElement("w:t")
        placeholder_text.text = placeholder
        placeholder_run.append(placeholder_text)
        run._element.append(fld_separate)
        run._element.append(placeholder_run)

    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._element.append(fld_end)


_PAGE_NUMBER_ALIGN = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
}


def _parse_page_number_position(position: str):
    """"top-right" → ("top", RIGHT). Неизвестное значение — ValueError сразу,
    а не тихий дефолт: опечатка в профиле не должна давать «почти верный» файл."""
    vert, _, horiz = (position or "").partition("-")
    if vert not in ("top", "bottom") or horiz not in _PAGE_NUMBER_ALIGN:
        raise ValueError(
            f"UniversityProfile.page_number_position={position!r}: ожидается "
            "'<top|bottom>-<left|center|right>', например 'bottom-center' "
            "или 'top-right'.")
    return vert, _PAGE_NUMBER_ALIGN[horiz]


def _add_page_number(section, *, hide_on_first=True,
                     position="bottom-center", size=FONT_SIZE_PAGE_NUMBER,
                     font=FONT_NAME):
    vert, align = _parse_page_number_position(position)
    # add_section() копирует sectPr предыдущей секции вместе с <w:titlePg/>
    # титульника — без явного False первая страница основного текста
    # (оглавление) брала пустой first-колонтитул и оставалась без номера.
    section.different_first_page_header_footer = hide_on_first

    part = section.header if vert == "top" else section.footer
    part.is_linked_to_previous = False

    for p in list(part.paragraphs):
        p.clear()
    p = part.paragraphs[0]
    p.alignment = align
    p.paragraph_format.line_spacing = LINE_SPACING_CODE

    # Каждая часть поля в своём run: если они в одном, LibreOffice теряет его
    # шрифт и кегль, и номер страницы выходит полужирным кеглем основного текста.
    for part in ("begin", "instr", "end"):
        run = p.add_run()
        _set_run_font(run, size=size, font=font)
        if part == "instr":
            el = OxmlElement("w:instrText")
            el.text = "PAGE"
        else:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), part)
        run._element.append(el)

    if hide_on_first:
        first = (section.first_page_header if vert == "top"
                 else section.first_page_footer)
        first.is_linked_to_previous = False
        for p in list(first.paragraphs):
            p.clear()


def _set_section_a4(section):
    section.page_width = PAGE_WIDTH
    section.page_height = PAGE_HEIGHT


def _apply_margins(section, left, right, top, bottom):
    section.left_margin = Mm(left)
    section.right_margin = Mm(right)
    section.top_margin = Mm(top)
    section.bottom_margin = Mm(bottom)


_ALIGN = {
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
    "both": WD_ALIGN_PARAGRAPH.JUSTIFY,
}


def _align_const(name: str):
    return _ALIGN.get(name, WD_ALIGN_PARAGRAPH.LEFT)


# Высота строки Times New Roman: ascent + descent ≈ 1,15 кегля. «Одна строка»
# основного текста при 14 пт и интервале 1,5 ≈ 24 пт.
_LINE_HEIGHT_EM = 1.15
_SPACE_RE = re.compile(
    r"^\s*(-?\d+(?:[.,]\d+)?)\s*(pt|пт|lines?|строк[аи]?|cm|см|mm|мм)?\s*$",
    re.IGNORECASE)


def _space_pt(value, line_pt: float) -> Optional[float]:
    """Интервал из профиля в пунктах. Число — пункты; строка с единицей:
    "1line" / "1.5 lines" / "1 строка" (строки основного текста), "12pt",
    "0.5cm", "5mm". None → None (поле не задано)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"интервал {value!r}: ожидается число или строка с единицей")
    if isinstance(value, (int, float)):
        return float(value)
    m = _SPACE_RE.match(str(value))
    if not m:
        raise ValueError(
            f"интервал {value!r}: ожидается число (пт) или «1line», «12pt», «0.5cm»")
    num = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "pt").lower()
    if unit.startswith(("line", "строк")):
        return float(round(num * line_pt))
    if unit in ("cm", "см"):
        return num * 72 / 2.54
    if unit in ("mm", "мм"):
        return num * 72 / 25.4
    return num


def _suppress_hyphens(ppr) -> None:
    """<w:suppressAutoHyphens/> на нужном месте схемы w:pPr: заголовки не переносят."""
    ppr.insert_element_before(
        OxmlElement("w:suppressAutoHyphens"),
        "w:kinsoku", "w:wordWrap", "w:overflowPunct", "w:topLinePunct",
        "w:autoSpaceDE", "w:autoSpaceDN", "w:bidi", "w:adjustRightInd",
        "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing",
        "w:mirrorIndents", "w:suppressOverlap", "w:jc", "w:textDirection",
        "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl", "w:divId",
        "w:cnfStyle", "w:rPr", "w:sectPr", "w:pPrChange",
    )


def _configure_heading_style(doc: _Document, level: int, size_pt: int,
                             align: str, *, line_spacing=LINE_SPACING_BODY,
                             space_after_pt: float = 12,
                             space_before_pt: float = 0,
                             indent_cm: float = 0,
                             bold: bool = True, italic: bool = False,
                             font: str = FONT_NAME):
    style = doc.styles[f"Heading {level}"]
    style.font.name = font
    style.font.size = Pt(size_pt)
    style.font.bold = bold
    if italic:
        style.font.italic = True
    style.font.color.rgb = RGBColor(0, 0, 0)
    _force_run_fonts(style.element.get_or_add_rPr(), font)

    pf = style.paragraph_format
    pf.alignment = _align_const(align)
    pf.line_spacing = line_spacing
    pf.space_before = Pt(space_before_pt)
    pf.space_after = Pt(space_after_pt)
    pf.first_line_indent = Cm(indent_cm) if indent_cm else Pt(0)
    pf.keep_with_next = True


# ============================================================
# Санитайзер пользовательской прозы
# ============================================================
#
# Любой текст, который агент передаёт в text/task/numbered/bullet/h1-h3 и
# в аргумент caption у figure/table, проходит через _sanitize_prose. Длинное
# и среднее тире — типичный AI-маркер и прямо запрещены writing-style блоком
# в SKILL.md, но модели всё равно их иногда вставляют. Подстраховываемся
# на уровне библиотеки: " — " / " – " между словами становится ", ",
# одиночные тире (например в диапазонах "1—5") — обычным дефисом.
#
# Авто-префиксы "Рисунок N — Описание" и "Таблица N — Описание" формирует
# сама библиотека — там тире обязательно по ГОСТ и не трогается.

_PROSE_DASH_BETWEEN_WORDS = re.compile(r"\s+[—–]\s+")
_PROSE_DASH_REMAINING = re.compile(r"[—–]")


def _sanitize_prose(text):
    if not text:
        return text
    text = _PROSE_DASH_BETWEEN_WORDS.sub(", ", text)
    text = _PROSE_DASH_REMAINING.sub("-", text)
    return text


def _mark_run_size(ppr, size) -> None:
    """Кегль знака абзаца (w:pPr/w:rPr/w:sz): им Word рисует номер или маркер
    списка, когда текст пунктов не основного кегля (приложения, 12 пт)."""
    rpr = ppr.find(qn("w:rPr"))
    if rpr is None:
        rpr = OxmlElement("w:rPr")
        ppr.insert_element_before(rpr, "w:sectPr", "w:pPrChange")
    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), str(int(round(size.pt * 2))))
    rpr.append(sz)


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русское согласование с числом: 1 источник, 2 источника, 5 источников."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


class RefNumber(str):
    """Составной номер рисунка, таблицы или формулы: «1.2» (по главам),
    «А.1» (в приложении). Строка, а не int: r.ref.* и f-строки печатают её
    как есть. При сквозной нумерации методы по-прежнему возвращают int."""
    __slots__ = ()


_CHOICES = {
    "heading_align_h1": ("center", "left", "right", "justify", "both"),
    "heading_align_h2": ("center", "left", "right", "justify", "both"),
    "heading_align_h3": ("center", "left", "right", "justify", "both"),
    "figure_caption_align": ("center", "left", "right", "justify"),
    "table_caption_align": ("center", "left", "right", "justify"),
    "table_header_align": ("", "center", "left", "right", "justify"),
    "keywords_align": ("center", "left", "right", "justify"),
    "bib_align": ("center", "left", "right", "justify"),
    "prose_dashes": ("strip", "normalize", "keep"),
    "nbsp": ("off", "required", "full"),
    "numbering": ("global", "chapter"),
    "formula_numbering": ("", "global", "chapter"),
    "list_layout": ("hanging", "paragraph"),
    "bib_style": ("7.1-2003", "7.0.100-2018"),
}
CHECK_NAMES = ("double-space", "heading-period", "paragraph-spacing",
               "table-mention", "figure-mention", "keywords-count", "bib-count")


def _validate_profile(profile: "UniversityProfile") -> None:
    """Опечатка в профиле — ValueError сразу при создании Report, а не тихий
    дефолт: «почти верный» файл хуже явной ошибки."""
    _parse_page_number_position(profile.page_number_position)
    if profile.title_student_layout not in ("group_line", "label_group"):
        raise ValueError(
            "UniversityProfile.title_student_layout="
            f"{profile.title_student_layout!r}: ожидается "
            "'group_line' или 'label_group'.")
    for name, allowed in _CHOICES.items():
        value = getattr(profile, name)
        if value not in allowed:
            raise ValueError(
                f"UniversityProfile.{name}={value!r}: ожидается одно из "
                + ", ".join(repr(a) for a in allowed if a != "") + ".")
    line_pt = profile.body_font_size * _LINE_HEIGHT_EM * profile.line_spacing
    for level in (1, 2, 3):
        for side in ("before", "after"):
            _space_pt(getattr(profile, f"heading_space_{side}_h{level}"), line_pt)
    _space_pt(profile.subtitle_space_after, line_pt)
    unknown = [c for c in profile.checks if c not in CHECK_NAMES + ("all",)]
    if unknown:
        raise ValueError(
            f"UniversityProfile.checks: неизвестные проверки {unknown}; "
            f"есть: {', '.join(CHECK_NAMES)} или 'all'.")
    if not profile.dash.strip():
        raise ValueError("UniversityProfile.dash: пустое тире")
    if not profile.list_letters or not profile.appendix_letters:
        raise ValueError("UniversityProfile: пустой list_letters / appendix_letters")


def validation_style(profile: "UniversityProfile", *, prose_mode: Optional[str] = None,
                     title_page: bool = True) -> Dict[str, Any]:
    """Правила проверки профиля для validate.py (пишутся в sentinel)."""
    return {
        "dash": profile.dash,
        "prose_dashes": prose_mode or profile.prose_dashes,
        "figure_label": profile.figure_label,
        "table_label": profile.table_label,
        "continuation_text": profile.table_continuation_text,
        "code_font": profile.code_font_name,
        "checks": list(profile.checks),
        "keywords_min": profile.keywords_min,
        "keywords_max": profile.keywords_max,
        "bib_min_sources": profile.bib_min_sources,
        "bib_title": profile.bib_title,
        "appendix_label": profile.appendix_label,
        "title_page": title_page,
    }


def _resolve_profile_arg(profile, *, use_env: bool = True) -> "UniversityProfile":
    """Report(profile=…): объект как есть; имя/путь — через styles.load_profile;
    None — GOST_REPORT_PROFILE из env и .gost-report.env, иначе ИТМО."""
    if isinstance(profile, UniversityProfile):
        return profile
    if profile is None:
        spec = (_load_gost_env().get(f"{_ENV_PREFIX}PROFILE", "").strip()
                if use_env else "")
        if not spec:
            return DEFAULT_PROFILE
        profile = spec
    import styles
    caller = _caller_file()
    search = [caller.parent] if caller else []
    return styles.load_profile(profile, search_dirs=search)


# LaTeX → OMML вынесено в модуль gost_report_math (см. research/19 §5).
# Формулы доступны через r.formula(...) (BC-shim → r.math.formula).


# ============================================================
# Класс отчёта — высокоуровневый API
# ============================================================

class Report:
    """Главный класс. Собирает документ из вызовов h1/text/code/figure/...

    Создание:
        r = Report(TitleConfig(...))                    # ITMO_PROFILE по умолчанию
        r = Report(TitleConfig(...), profile=GOST_PROFILE)
        r = Report(TitleConfig(...), profile=UniversityProfile(...))
        r = Report(title_page=False)                    # без титульника (TitleConfig опционален)

    Без титульного листа: `Report(title_page=False)` (или env
    `GOST_REPORT_NO_TITLE_PAGE=1`) — документ начинается сразу с основного
    текста, поля основного текста, нумерация с первой страницы. Поля
    TitleConfig в этом режиме не обязательны (валидация work_type/topic/ФИО/
    группы/года пропускается).

    Контент: см. методы toc/h1/h2/h3/text/task/code/figure/table/numbered/bullet.
    Сохранение: r.save("report.docx").
    """

    def __init__(self, title: Optional[TitleConfig] = None,
                 profile: Union[UniversityProfile, str, Path, None] = None,
                 *,
                 title_page: bool = True,
                 project_root: Optional[Union[str, Path]] = None,
                 typography: bool = False,
                 allow_dashes: bool = False,
                 use_env: bool = True):
        self._doc = Document()
        self._typography = typography
        self._allow_dashes = allow_dashes
        self._saved_docx: Optional[Path] = None
        self._use_env = use_env
        # Титульник можно отключить: параметр title_page=False или env
        # GOST_REPORT_NO_TITLE_PAGE=1. Env только отключает (не включает),
        # чтобы автоматизация могла гасить титул, не трогая build.py.
        self._has_title_page = title_page and not (use_env and _env_flag("NO_TITLE_PAGE"))
        # Env побеждает build.py: накладываем перед валидацией обязательных
        # полей, чтобы можно было опускать FIO/group/year в TitleConfig.
        # use_env=False: только то, что передано в TitleConfig (работа за
        # другого человека без правки os.environ в build.py).
        title = title if title is not None else TitleConfig()
        self._title = _apply_env_overrides(title) if use_env else title
        # profile: объект, имя стиля («mbi»), путь к .toml/.json или None —
        # тогда GOST_REPORT_PROFILE из env/.gost-report.env, иначе ИТМО.
        profile = _resolve_profile_arg(profile, use_env=use_env)
        _validate_profile(profile)
        self._profile = profile
        # Обязательные поля титула нужны только когда титул строится.
        if self._has_title_page:
            self._check_title_fields()
        # Типографика из профиля. Pt/Cm от тех же чисел, что и прежние
        # константы, — у ITMO/GOST байты XML не меняются.
        self._font = profile.font_name
        self._body_size = Pt(profile.body_font_size)
        self._line_spacing = profile.line_spacing
        self._first_line_indent = Cm(profile.first_line_indent_cm)
        self._space_block = Pt(profile.block_space_pt)
        self._title_large = Pt(profile.title_size_large)
        self._right_block_indent = Cm(profile.title_right_block_indent_cm)
        # «Одна строка» основного текста — единица интервалов "1line" в профиле.
        self._line_pt = profile.body_font_size * _LINE_HEIGHT_EM * profile.line_spacing
        self._prose_mode = "keep" if allow_dashes else profile.prose_dashes
        self._markup = typography or profile.markup
        self._formula_space = (Pt(profile.formula_space_pt)
                               if profile.formula_space_pt is not None
                               else self._space_block)
        self._figure_counter = 0
        self._table_counter = 0
        self._formula_counter = 0
        # Нумерация по главам и приложениям: префикс номера («1.», «А.»).
        self._chapter: Optional[str] = None
        self._appendix: Optional[str] = None
        self._appendices: List[str] = []
        self._heading_numbers = [0, 0, 0]
        self._prev_kind = ""
        self._prev_heading = None
        self._in_chapter = False
        self._volume_slots: List[Any] = []
        self._pages_runs: List[Any] = []
        self._kind_totals = {"figure": 0, "table": 0, "formula": 0}
        self._labels: Dict[str, str] = {}
        self._abstract_ids: Dict[str, str] = {}
        self._continuation: List[Any] = []
        self._just_broke_page = False
        self._break_paragraphs: list = []
        self._next_num_id = 100

        # Подключаемые модули (r.plot, r.diagram, …). discover() лёгкий — тянет
        # лишь классы модулей (core_api), тяжёлые пакеты грузятся при первом
        # использовании namespace через __getattr__ → attach(). См. registry.py.
        try:
            from registry import discover as _discover_modules
            self._modules = _discover_modules()
        except Exception:
            self._modules = {}
        self._attached = {}
        self._tmpdir = None

        # Авто-резолв путей: если project_root не задан — paths() обходит
        # стек вверх до user-скрипта, оттуда ищет маркер проекта (.git,
        # Makefile, pyproject.toml, .claude). Используется figure() для
        # резолва относительных имён файлов и save() для дефолтного пути.
        if project_root is None:
            self._paths = paths()
        else:
            self._paths = paths(Path(project_root))

        # 1. Базовые стили
        _ensure_normal_style(self._doc, size=self._body_size,
                             line_spacing=self._line_spacing, font=self._font)
        for level in (1, 2, 3):
            before = _space_pt(getattr(profile, f"heading_space_before_h{level}"),
                               self._line_pt)
            after = _space_pt(getattr(profile, f"heading_space_after_h{level}"),
                              self._line_pt)
            _configure_heading_style(
                self._doc, level,
                getattr(profile, f"heading_size_h{level}"),
                getattr(profile, f"heading_align_h{level}"),
                line_spacing=profile.heading_line_spacing or self._line_spacing,
                space_after_pt=(profile.heading_space_after_pt
                                if after is None else after),
                space_before_pt=0 if before is None else before,
                indent_cm=getattr(profile, f"heading_indent_cm_h{level}"),
                bold=getattr(profile, f"heading_bold_h{level}"),
                italic=getattr(profile, f"heading_italic_h{level}"),
                font=self._font,
            )

        page_number = dict(position=profile.page_number_position,
                           size=Pt(profile.page_number_size), font=self._font)
        if self._has_title_page:
            # 2. Секция титульника (первая)
            section = self._doc.sections[0]
            _set_section_a4(section)
            _apply_margins(section,
                           profile.title_margin_left, profile.title_margin_right,
                           profile.title_margin_top, profile.title_margin_bottom)
            _add_page_number(section, hide_on_first=True, **page_number)
            if profile.title_builder is not None:
                profile.title_builder(self)
            elif profile.title_docx:
                import title_template
                title_template.import_title_docx(self, profile.title_docx)
            elif profile.title_blocks:
                import title_template
                title_template.render_blocks(self, profile.title_blocks)
            else:
                self._build_title_page()

            # 3. Новая секция для основного текста
            body_section = self._doc.add_section(WD_SECTION.NEW_PAGE)
            _set_section_a4(body_section)
            _apply_margins(body_section,
                           profile.body_margin_left, profile.body_margin_right,
                           profile.body_margin_top, profile.body_margin_bottom)
            _add_page_number(body_section, hide_on_first=False, **page_number)
        else:
            # Без титульника: единственная секция сразу с полями основного
            # текста; нумерация страниц видна с первой страницы.
            section = self._doc.sections[0]
            _set_section_a4(section)
            _apply_margins(section,
                           profile.body_margin_left, profile.body_margin_right,
                           profile.body_margin_top, profile.body_margin_bottom)
            _add_page_number(section, hide_on_first=False, **page_number)
        self._just_broke_page = True
        if typography:
            self._apply_typography()
        else:
            if profile.auto_hyphenation:
                self._apply_hyphenation()
            elif profile.language:
                self._set_language(profile.language)
            if profile.nbsp != "off":
                self._set_compat_mode_15()
            if profile.widow_control:
                self._doc.styles["Normal"].paragraph_format.widow_control = True

    def _set_language(self, lang: str) -> None:
        """Язык документа (w:lang в docDefaults): без него Word и LibreOffice
        не переносят русский текст."""
        rpr = (self._doc.styles.element.find(qn("w:docDefaults"))
               .find(qn("w:rPrDefault")).find(qn("w:rPr")))
        el = rpr.find(qn("w:lang"))
        if el is None:
            el = OxmlElement("w:lang")
            rpr.append(el)
        el.set(qn("w:val"), lang)

    def _set_hyphenation_settings(self) -> None:
        settings = self._doc.settings.element
        anchor = settings.find(qn("w:defaultTabStop"))
        for tag, value in reversed((("w:autoHyphenation", "true"),
                                    ("w:consecutiveHyphenLimit", "2"),
                                    ("w:hyphenationZone", "284"),
                                    ("w:doNotHyphenateCaps", "true"))):
            el = OxmlElement(tag)
            el.set(qn("w:val"), value)
            anchor.addnext(el)

    def _set_compat_mode_15(self) -> None:
        # Режим Word 2013+: иначе неразрывные пробелы не тянутся при выключке по ширине.
        for compat in self._doc.settings.element.iter(qn("w:compatSetting")):
            if compat.get(qn("w:name")) == "compatibilityMode":
                compat.set(qn("w:val"), "15")

    def _apply_hyphenation(self) -> None:
        """profile.auto_hyphenation без режима typography: язык, автоперенос,
        заголовки без переносов. Остальная полировка typography не включается."""
        self._set_language(self._profile.language or "ru-RU")
        self._set_hyphenation_settings()
        for level in (1, 2, 3):
            _suppress_hyphens(
                self._doc.styles[f"Heading {level}"].element.get_or_add_pPr())

    def _apply_typography(self) -> None:
        """Настройки docx для typography=True: язык, переносы, воздух вокруг заголовков."""
        doc = self._doc
        # Без русского языка документа Word и LibreOffice не переносят текст.
        self._set_language("ru-RU")
        self._set_hyphenation_settings()
        self._set_compat_mode_15()
        if self._profile.language:
            self._set_language(self._profile.language)

        doc.styles["Normal"].paragraph_format.widow_control = True
        # Заголовок ближе к своему тексту, чем к предыдущему: отступ сверху втрое
        # больше нижнего. 1,15 — естественная строка Times при одинарном интервале.
        # Явные heading_space_* профиля побеждают этот расчёт.
        line = self._profile.body_font_size * 1.15 * self._line_spacing
        for level in (1, 2, 3):
            style = doc.styles[f"Heading {level}"]
            pf = style.paragraph_format
            pf.line_spacing = self._profile.heading_line_spacing or 1.0
            pf.keep_together = True
            if getattr(self._profile, f"heading_space_before_h{level}") is None:
                pf.space_before = Pt(0 if level == 1 else round(line))
            if getattr(self._profile, f"heading_space_after_h{level}") is None:
                pf.space_after = Pt(round(line / 2 if level == 1 else line / 3))
            _suppress_hyphens(style.element.get_or_add_pPr())

    def _clean(self, text: str) -> str:
        """Единая обработка прозы: тире по режиму профиля (prose_dashes) и
        неразрывные пробелы (typography / nbsp)."""
        mode = self._prose_mode
        if mode == "strip":
            text = _sanitize_prose(text)
        elif mode == "normalize":
            text = normalize_dashes(text, self._profile.dash)
        if self._typography:
            text = typograf(text, dashes=self._allow_dashes)
        elif self._profile.nbsp == "required":
            text = required_nbsp(text)
        elif self._profile.nbsp == "full":
            text = typograf(text)
        return text

    def _clean_display(self, text: str, *, glue: bool) -> str:
        """Проза заголовка или подписи: _clean + предлоги не в конце строки."""
        text = self._clean(text)
        return glue_short_words(text) if glue else text

    # --------------------------------------------------------
    # Внутреннее: построение титульника
    # --------------------------------------------------------

    @staticmethod
    def _check_required(cfg: TitleConfig) -> None:
        """После применения env-override проверяет, что обязательные по
        смыслу поля заданы. Сообщение указывает на оба источника — build.py
        и env, чтобы пользователь сразу понимал где править."""
        def _missing(value: str) -> bool:
            return not value or _is_placeholder(value)

        problems = []
        if _missing(cfg.work_type):
            problems.append("work_type (GOST_REPORT_WORK_TYPE)")
        if _missing(cfg.topic):
            problems.append("topic (GOST_REPORT_TOPIC)")
        if _missing(cfg.student_name) and not cfg.student_names:
            problems.append(
                "student_name или student_names "
                "(GOST_REPORT_STUDENT_NAME / GOST_REPORT_STUDENT_NAMES)"
            )
        if _missing(cfg.student_group):
            problems.append("student_group (GOST_REPORT_STUDENT_GROUP)")
        if _missing(cfg.year):
            problems.append("year (GOST_REPORT_YEAR)")
        if problems:
            raise ValueError(
                "TitleConfig: не заданы обязательные поля: "
                + ", ".join(problems)
                + f". Заполни их в {_config_file_path()} (env-формат), "
                  "в <курс>/.gost-report.env, через переменные окружения "
                  "или прямо в TitleConfig(...). "
                  "Значения-заглушки «ЗАПОЛНИ…» считаются незаполненными."
            )

    def _check_title_fields(self) -> None:
        """Обязательные поля титула. Встроенная раскладка и title_builder —
        прежний набор (_check_required); шаблонный титул (title_blocks /
        title_docx) — profile.title_required."""
        p = self._profile
        if p.title_builder is None and (p.title_blocks or p.title_docx):
            missing = [f for f in p.title_required if not self.title_value(f)]
            if missing:
                raise ValueError(
                    "TitleConfig: для титула профиля "
                    f"«{p.name or 'без имени'}» не заданы поля: "
                    + ", ".join(f"{f} (GOST_REPORT_{f.upper()})" for f in missing)
                    + ". Заполни их в TitleConfig(...), в extra={...} или в env.")
            return
        self._check_required(self._title)

    def title_value(self, name: str) -> str:
        """Значение {поля} для титула: TitleConfig, затем TitleConfig.extra,
        затем профиль (university_*, faculty, city, ministry). Заглушки
        «ЗАПОЛНИ…» считаются пустыми."""
        cfg = self._title
        value = ""
        if name in ("student_label",):
            value = _resolve_student_label(cfg)
        elif name == "work_title":
            value = f"{cfg.work_type} {cfg.work_number}".strip()
        elif name == "students":
            names = cfg.student_names or ([cfg.student_name] if cfg.student_name else [])
            value = "\n".join(names)
        elif name == "teacher_line":
            value = " ".join(x for x in (cfg.teacher_degree, cfg.teacher_position,
                                         cfg.teacher_name) if x)
        elif isinstance(getattr(cfg, name, None), str):
            value = getattr(cfg, name)
        if not value:
            value = (cfg.extra or {}).get(name, "")
        if not value:
            prof = getattr(self._profile, name, "")
            value = prof if isinstance(prof, str) else ""
        return "" if _is_placeholder(value) else value

    def _resolve(self, attr: str) -> str:
        """TitleConfig.<attr> если задано, иначе UniversityProfile.<attr>."""
        value = getattr(self._title, attr, "") or ""
        if value:
            return value
        return getattr(self._profile, attr, "") or ""

    def _printable_cm(self) -> float:
        """Печатная ширина A4 минус поля активного профиля, в см.
        Используется figure() для clamp ширины картинки и formula() для
        размещения номера формулы на правом крае."""
        return (PAGE_WIDTH_MM
                - self._profile.body_margin_left
                - self._profile.body_margin_right) / 10.0

    # --------------------------------------------------------
    # Подключаемые модули: lazy-attach + CoreServices-фасад
    # --------------------------------------------------------
    def __getattr__(self, name: str):
        """Ленивое присоединение модуля при первом обращении к r.<namespace>.
        Срабатывает только если обычный lookup не нашёл атрибут. Ошибка
        зависимостей (ActionableImportError из check_available) пробрасывается
        наружу как есть, не превращается в AttributeError."""
        if name.startswith("_"):
            raise AttributeError(name)
        modules = self.__dict__.get("_modules") or {}
        if name in modules:
            attached = self.__dict__.setdefault("_attached", {})
            if name not in attached:
                attached[name] = modules[name].attach(self)
            return attached[name]
        raise AttributeError(name)

    def printable_cm(self) -> float:
        """Публичный фасад _printable_cm() для модулей."""
        return self._printable_cm()

    def sanitize(self, text: str) -> str:
        """Обработка прозы (подписи/контент) для модулей, см. _clean."""
        return self._clean(text)

    def nbsp_only(self, text: str) -> str:
        """Только неразрывные пробелы профиля (nbsp), без замены тире: для
        текста, где тире обязательны (записи списка источников)."""
        mode = self._profile.nbsp
        if mode == "required":
            return required_nbsp(text)
        if mode == "full":
            return typograf(text)
        return text

    def next_formula_number(self) -> Union[int, "RefNumber"]:
        """Следующий номер формулы (для math-модуля): int при сквозной
        нумерации, RefNumber «1.2» / «А.1» по главам и в приложениях."""
        return self._next_number("formula")

    # ---- нумерация рисунков, таблиц, формул ----
    def _numbering_mode(self, kind: str) -> str:
        p = self._profile
        if kind == "formula" and p.formula_numbering:
            return p.formula_numbering
        return p.numbering

    def _number_prefix(self, kind: str) -> Optional[str]:
        if self._appendix:
            return self._appendix
        if self._numbering_mode(kind) == "chapter" and self._chapter:
            return self._chapter
        return None

    def _next_number(self, kind: str):
        attr = f"_{kind}_counter"
        n = getattr(self, attr) + 1
        setattr(self, attr, n)
        self._kind_totals[kind] += 1
        prefix = self._number_prefix(kind)
        return RefNumber(f"{prefix}.{n}") if prefix else n

    def _reset_counters(self, *, chapter_only: bool) -> None:
        for kind in ("figure", "table", "formula"):
            if not chapter_only or self._numbering_mode(kind) == "chapter":
                setattr(self, f"_{kind}_counter", 0)

    def make_paragraph(self, **kwargs):
        """Пустой параграф с форматированием — фасад _make_paragraph (для
        модулей, строящих низкоуровневую раскладку, напр. формулы)."""
        return self._make_paragraph(**kwargs)

    def append_inline(self, p, raw, **kwargs) -> None:
        """Дописать в абзац прозу с инлайн-математикой — фасад _append_inline."""
        self._append_inline(p, raw, **kwargs)

    def add_text_paragraph(self, text: str = "", **kwargs):
        """Текстовый параграф — фасад _add_paragraph."""
        return self._add_paragraph(text, **kwargs)

    def add_runs_paragraph(self, runs: Sequence[StyledRun], **kwargs):
        """Абзац из нескольких StyledRun (метка подчёркнута, значение нет и т.п.)
        — для profile.title_builder. kwargs: align, left_indent."""
        return self._add_runs_paragraph(runs, **kwargs)

    def add_bottom_block(self, lines: Sequence[str]):
        """Строки по центру, прижатые к нижнему полю страницы (город/год на
        титуле) — для profile.title_builder."""
        return self._add_bottom_anchored_block(list(lines))

    def add_blank_paragraphs(self, count: int) -> None:
        """Пустые абзацы-разделители — для profile.title_builder."""
        self._add_blank_paragraphs(count)

    @property
    def title_config(self) -> TitleConfig:
        """TitleConfig после наложения env — для profile.title_builder."""
        return self._title

    def set_run_font(self, run, **kwargs) -> None:
        """Шрифт для run — фасад _set_run_font. Без size — кегль основного
        текста активного профиля (в приложении — appendix_font_size)."""
        if kwargs.get("size") is None:
            kwargs["size"] = self._text_size()
        kwargs.setdefault("font", self._font)
        _set_run_font(run, **kwargs)

    def _font_run(self, run, **kwargs) -> None:
        kwargs.setdefault("font", self._font)
        _set_run_font(run, **kwargs)

    def _text_run(self, p, text: str, **fmt):
        """Run с текстом и шрифтом. Неразрывный дефис (U+2011 от nbsp="required"
        и r.keywords) пишется элементом <w:noBreakHyphen/>: Word рисует его
        глифом обычного дефиса и не переносит строку на нём. Возвращает первый run."""
        if NBHY not in text:
            run = p.add_run(text)
            self._font_run(run, **fmt)
            return run
        first = None
        parts = text.split(NBHY)
        for k, part in enumerate(parts):
            run = p.add_run(part) if part else p.add_run()
            self._font_run(run, **fmt)
            if k < len(parts) - 1:
                run._r.append(OxmlElement("w:noBreakHyphen"))
            first = first or run
        return first

    def add_text_runs(self, p, text: str, **fmt):
        """Публичный фасад _text_run для модулей (запись списка источников)."""
        fmt.setdefault("size", self._text_size())
        return self._text_run(p, text, **fmt)

    def _text_size(self):
        """Кегль прозы в текущем месте документа: в приложении —
        profile.appendix_font_size, если задан."""
        if self._appendix and self._profile.appendix_font_size:
            return Pt(self._profile.appendix_font_size)
        return self._body_size

    def _size_or_text(self, pt: Optional[float]):
        return Pt(pt) if pt else self._text_size()

    @property
    def formula_space(self):
        """Интервал до и после формулы (formula_space_pt, иначе block_space_pt)."""
        return self._formula_space

    @property
    def line_pt(self) -> float:
        """Высота строки основного текста, пт: единица "1line" в профиле."""
        return self._line_pt

    def space(self, value) -> Optional[Length]:
        """Интервал профиля ("1line", "12pt", число пт) → Length."""
        pt = _space_pt(value, self._line_pt)
        return None if pt is None else Pt(pt)

    @property
    def space_block(self):
        """Межблочный интервал профиля (block_space_pt) для модулей."""
        return self._space_block

    @property
    def body_size(self):
        """Кегль основного текста профиля (Length) для модулей и title_builder."""
        return self._body_size

    def embed_figure(self, image, caption: str, *,
                     width_cm: Optional[float] = None):
        """Единая точка нумерации+подписи рисунков для модулей. Принимает путь
        ИЛИ Figure-объект (chart/diagram). Возвращает номер рисунка."""
        return self.figure(image, caption, width_cm=width_cm)

    def embed_table(self, rows: Sequence[Sequence[str]], caption: str = "",
                    *, has_header: bool = True):
        """Единая точка нумерации+подписи таблиц для модулей."""
        return self.table(rows, caption=caption, has_header=has_header)

    @property
    def tmp_dir(self) -> Path:
        """Временная директория для PNG, сгенерированных модулями. Чистится в
        save()/teardown. Путь в .docx не утекает — картинки встраиваются байтами."""
        if self.__dict__.get("_tmpdir") is None:
            import tempfile
            self._tmpdir = Path(tempfile.mkdtemp(prefix="gost-report-"))
        return self._tmpdir

    @property
    def paths(self):
        """ProjectPaths активного проекта (root/docs/figures/...)."""
        return self._paths

    def _cleanup(self) -> None:
        """Idempotent teardown подключённых модулей + удаление tmp-директории."""
        modules = self.__dict__.get("_modules") or {}
        for ns in list(self.__dict__.get("_attached") or {}):
            try:
                modules[ns].teardown()
            except Exception:
                pass
        tmp = self.__dict__.get("_tmpdir")
        if tmp:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
            self._tmpdir = None

    def __del__(self):
        try:
            self._cleanup()
        except Exception:
            pass

    def _make_paragraph(self, *, align=WD_ALIGN_PARAGRAPH.CENTER,
                        line_spacing=None,
                        space_before=Pt(0), space_after=Pt(0),
                        left_indent=None, first_line_indent=None):
        """Создаёт пустой параграф с заданным форматированием.

        Единая точка входа для всех «телом-уровневых» методов
        (text/task/figure/formula/table/caption/list). Возвращает Paragraph
        без runs — caller добавляет содержимое (текст, OMML, picture, и т.п.).

        Любой параметр (``align``, ``line_spacing``, ``space_before``,
        ``space_after``, ``left_indent``, ``first_line_indent``) можно явно
        обнулить через ``_INHERIT`` чтобы оставить значение из стиля абзаца
        (нужно для list-параграфов: их ``w:before`` берётся из стиля
        ``List Number`` / ``List Bullet``). ``line_spacing=None`` — интервал
        основного текста профиля.
        """
        if line_spacing is None:
            line_spacing = self._line_spacing
        p = self._doc.add_paragraph()
        if align is not _INHERIT:
            p.alignment = align
        pf = p.paragraph_format
        if line_spacing is not _INHERIT:
            pf.line_spacing = line_spacing
        if space_before is not _INHERIT:
            pf.space_before = space_before
        if space_after is not _INHERIT:
            pf.space_after = space_after
        if left_indent is not None and left_indent is not _INHERIT:
            pf.left_indent = left_indent
        if first_line_indent is not None and first_line_indent is not _INHERIT:
            pf.first_line_indent = first_line_indent
        return p

    def _add_paragraph(self, text="", *, align=WD_ALIGN_PARAGRAPH.CENTER,
                       size=None, bold=False, italic=False,
                       underline=False, left_indent=None,
                       first_line_indent=None,
                       line_spacing=None,
                       space_before=Pt(0), space_after=Pt(0)):
        p = self._make_paragraph(align=align, line_spacing=line_spacing,
                                 space_before=space_before,
                                 space_after=space_after,
                                 left_indent=left_indent,
                                 first_line_indent=first_line_indent)
        if text:
            self._text_run(p, text, size=self._text_size() if size is None else size,
                           bold=bold, italic=italic, underline=underline)
        return p

    # Инлайновая математика: $...$ внутри прозы → нативное Word-уравнение.
    # \$ — литеральный знак доллара. (?<!\\) — не считать экранированный $.
    _INLINE_MATH_RE = re.compile(r"(?<!\\)\$(.+?)(?<!\\)\$", re.DOTALL)

    def _append_inline(self, p, raw, *, size=None, bold=False,
                       italic=False, underline=False, markup=None,
                       clean=None):
        """Заполняет параграф p: проза + инлайновая математика из $...$.

        Без '$' — ровно один run (байт-в-байт как раньше). LaTeX внутри $...$
        не санируется (синтаксис не искажается) и рендерится в инлайновый
        <m:oMath>; прозовые сегменты проходят _clean. \\$ → '$'.
        markup=None — разметка **жирный** / *курсив* по профилю (markup или
        typography); clean — своя обработка прозы вместо _clean."""
        if not raw:
            return
        if size is None:
            size = self._text_size()
        use_markup = self._markup if markup is None else markup
        clean = clean or self._clean

        def _prose(s):
            if not s:
                return
            s = clean(s).replace("\\$", "$")
            parts = split_emphasis(s) if use_markup else [(s, False, False)]
            for chunk, strong, emphasis in parts:
                self._text_run(p, chunk, size=size, bold=bold or strong,
                               italic=italic or emphasis, underline=underline)

        if "$" not in raw:
            _prose(raw)
            return
        from gost_report_math._omml import latex_to_omath
        pos = 0
        for m in self._INLINE_MATH_RE.finditer(raw):
            _prose(raw[pos:m.start()])
            p._p.append(latex_to_omath(m.group(1)))
            pos = m.end()
        _prose(raw[pos:])

    def add_inline_paragraph(self, raw="", *, align=WD_ALIGN_PARAGRAPH.CENTER,
                             size=None, bold=False, italic=False,
                             underline=False, left_indent=None,
                             first_line_indent=None,
                             line_spacing=None,
                             space_before=Pt(0), space_after=Pt(0),
                             markup=None):
        """Как _add_paragraph, но текст поддерживает инлайновую математику $...$.
        Публичный core-сервис (используется text/task и math-модулем для where)."""
        p = self._make_paragraph(align=align, line_spacing=line_spacing,
                                 space_before=space_before,
                                 space_after=space_after,
                                 left_indent=left_indent,
                                 first_line_indent=first_line_indent)
        self._append_inline(p, raw, size=size, bold=bold, italic=italic,
                            underline=underline, markup=markup)
        return p

    def _add_caption(self, prefix, caption, *, align,
                     space_before=Pt(0), space_after=Pt(0),
                     first_line_indent=Pt(0)):
        """Подпись «Рисунок/Таблица N — …»: префикс с обязательным тире (литерал,
        не санируется) + тело подписи с поддержкой инлайн-математики $...$.
        Без '$' — один run (байты как раньше). Кегль, интервал, переносы и
        склейка предлогов — поля профиля caption_*."""
        prof = self._profile
        size = self._size_or_text(prof.caption_font_size)
        line_spacing = prof.caption_line_spacing
        clean = (lambda t: self._clean_display(t, glue=True)) if prof.caption_glue else None
        if "$" not in caption:
            body = clean(caption) if clean else self._clean(caption)
            p = self._add_paragraph(
                prefix + body, align=align, size=size,
                first_line_indent=first_line_indent, line_spacing=line_spacing,
                space_before=space_before, space_after=space_after)
        else:
            p = self._make_paragraph(align=align, first_line_indent=first_line_indent,
                                     line_spacing=line_spacing,
                                     space_before=space_before, space_after=space_after)
            pre = p.add_run(prefix)
            self._font_run(pre, size=size)
            self._append_inline(p, caption, size=size, clean=clean)
        if not prof.caption_hyphenation:
            _suppress_hyphens(p._p.get_or_add_pPr())
        return p

    def _caption_prefix(self, label: str, number) -> str:
        return f"{label} {number} {self._profile.dash} "

    def _add_runs_paragraph(self, runs, *, align=WD_ALIGN_PARAGRAPH.CENTER,
                            left_indent=None):
        p = self._make_paragraph(align=align, left_indent=left_indent)
        for r in runs:
            run = p.add_run(r.text)
            # Explicit None-check, не `or`-fallback: иначе Pt(0) (бывает
            # falsy) был бы перетёрт дефолтом.
            run_size = r.size if r.size is not None else self._body_size
            self._font_run(
                run,
                size=run_size,
                bold=r.bold,
                italic=r.italic,
                underline=r.underline,
            )
        return p

    def _add_bottom_anchored_block(self, lines):
        """Borderless single-column таблица, плавающе прижатая к нижнему
        полю текущей страницы. Каждый элемент ``lines`` — отдельная строка
        (центр, TNR 14). Используется для города/года на титульнике, чтобы
        они оставались на 1-й странице независимо от длины шапки/темы/ФИО.

        Реализация — через OOXML ``w:tblpPr`` (плавающее позиционирование):
        ``vertAnchor=margin`` + ``tblpYSpec=bottom`` прижимает низ таблицы к
        нижнему полю; ``horzAnchor=margin`` + ``tblpXSpec=center`` центрирует
        по горизонтали внутри полей. python-docx высокоуровневого API для
        этого не даёт, поэтому правим XML напрямую.
        """
        table = self._doc.add_table(rows=len(lines), cols=1)
        table.autofit = True

        tbl = table._element
        tblPr = tbl.find(qn("w:tblPr"))
        if tblPr is None:
            tblPr = OxmlElement("w:tblPr")
            tbl.insert(0, tblPr)

        tblpPr = OxmlElement("w:tblpPr")
        tblpPr.set(qn("w:vertAnchor"), "margin")
        tblpPr.set(qn("w:horzAnchor"), "margin")
        tblpPr.set(qn("w:tblpYSpec"), "bottom")
        tblpPr.set(qn("w:tblpXSpec"), "center")
        tblpPr.set(qn("w:leftFromText"), "0")
        tblpPr.set(qn("w:rightFromText"), "0")
        tblpPr.set(qn("w:topFromText"), "0")
        tblpPr.set(qn("w:bottomFromText"), "0")
        tblPr.append(tblpPr)

        tblOverlap = OxmlElement("w:tblOverlap")
        tblOverlap.set(qn("w:val"), "never")
        tblPr.append(tblOverlap)

        tblBorders = OxmlElement("w:tblBorders")
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            b = OxmlElement(f"w:{edge}")
            b.set(qn("w:val"), "nil")
            tblBorders.append(b)
        tblPr.append(tblBorders)

        for row, text in zip(table.rows, lines):
            cell = row.cells[0]
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            pf = p.paragraph_format
            pf.line_spacing = self._line_spacing
            pf.space_before = Pt(0)
            pf.space_after = Pt(0)
            run = p.add_run(text)
            self._font_run(run, size=self._body_size)

        return table

    def _build_title_page(self):
        self._title_header()
        self._add_blank_paragraphs(_TITLE_TOP_GAP)
        self._title_work_block()
        self._add_blank_paragraphs(_TITLE_MID_GAP)
        self._title_student_block()
        self._title_teacher_block()
        self._title_footer()

    def _add_blank_paragraphs(self, count: int) -> None:
        for _ in range(count):
            self._add_paragraph()

    def _title_header(self) -> None:
        """Министерство → университет → факультет."""
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

    def _title_work_block(self) -> None:
        """Вид работы (+ номер) → дисциплина → тема → вариант."""
        cfg = self._title
        work_type_text = cfg.work_type
        if cfg.work_number:
            work_type_text = f"{work_type_text} {cfg.work_number}".strip()
        self._add_paragraph(work_type_text, size=self._title_large,
                            bold=True)
        if cfg.discipline:
            self._add_paragraph(f"по дисциплине «{cfg.discipline}»",
                                size=self._title_large)
        if cfg.topic:
            self._add_paragraph(cfg.topic, size=self._title_large)
        if cfg.variant:
            self._add_paragraph()
            self._add_paragraph(f"Вариант №{cfg.variant}",
                                size=self._title_large)

    def _title_student_block(self) -> None:
        """Правый блок студента, раскладка — profile.title_student_layout:
        group_line:  «Группа: …» → «Выполнил(и): ФИО», доп. имена ниже;
        label_group: «Выполнила: ученица 9 «Б» класса» → все ФИО строками ниже."""
        cfg = self._title
        group_text = self._profile.title_group_template.format(
            group=cfg.student_group)
        student_label = _resolve_student_label(cfg)
        names = cfg.student_names or ([cfg.student_name] if cfg.student_name else [])
        block_indent = self._right_block_indent
        # group_line: доп. имена — под текст после «Выполнили: » (ширина метки
        # ≈ 2.2 см в TNR 14, пропорционально кеглю профиля).
        rest_indent = block_indent + Cm(
            _RIGHT_BLOCK_REST_INDENT.cm * self._profile.body_font_size / 14)

        if self._profile.title_student_layout == "label_group":
            self._add_runs_paragraph(
                [
                    StyledRun(student_label, underline=True),
                    StyledRun(f": {group_text}"),
                ],
                align=WD_ALIGN_PARAGRAPH.LEFT,
                left_indent=self._right_block_indent,
            )
            rest = names
            rest_indent = block_indent
        else:
            self._add_runs_paragraph(
                [StyledRun(group_text, underline=True)],
                align=WD_ALIGN_PARAGRAPH.LEFT,
                left_indent=self._right_block_indent,
            )
            first, *rest = names
            self._add_runs_paragraph(
                [
                    StyledRun(student_label, underline=True),
                    StyledRun(f": {first}"),
                ],
                align=WD_ALIGN_PARAGRAPH.LEFT,
                left_indent=self._right_block_indent,
            )
        for extra_name in rest:
            self._add_runs_paragraph(
                [StyledRun(extra_name)],
                align=WD_ALIGN_PARAGRAPH.LEFT,
                left_indent=rest_indent,
            )

    def _title_teacher_block(self) -> None:
        """Преподаватель: метка → "<степень> <должность> ФИО"."""
        cfg = self._title
        if not cfg.teacher_name:
            return
        self._add_paragraph(left_indent=self._right_block_indent,
                            align=WD_ALIGN_PARAGRAPH.LEFT)
        self._add_runs_paragraph(
            [
                StyledRun(cfg.teacher_label, underline=True),
                StyledRun(":"),
            ],
            align=WD_ALIGN_PARAGRAPH.LEFT,
            left_indent=self._right_block_indent,
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
            left_indent=self._right_block_indent,
        )

    def _title_footer(self) -> None:
        """Город и год — плавающая borderless-таблица, прижатая к нижнему
        полю чтобы не уезжать на page 2 при длинной шапке/теме/ФИО."""
        cfg = self._title
        city = self._resolve("city")
        footer_lines = []
        if city:
            footer_lines.append(city)
        footer_lines.append(cfg.year)
        self._add_bottom_anchored_block(footer_lines)

    # --------------------------------------------------------
    # Публичный API: контент основного текста
    # --------------------------------------------------------

    def _enable_update_fields_on_open(self):
        """Ставит <w:updateFields w:val="true"/> в settings.xml. Word, Pages
        и LibreOffice при первом открытии файла обновят все поля (включая
        TOC и нумерацию). Идемпотентно: повторный r.toc() не дублирует."""
        settings = self._doc.settings.element
        existing = settings.find(qn("w:updateFields"))
        if existing is None:
            uf = OxmlElement("w:updateFields")
            uf.set(qn("w:val"), "true")
            settings.insert(0, uf)
        elif existing.get(qn("w:val")) != "true":
            existing.set(qn("w:val"), "true")

    # ---- метки и ссылки вперёд ----
    _REF_TOKEN_RE = re.compile("\u27e6([^\u27e6\u27e7]+)\u27e7")

    def set_label(self, label: str, number) -> None:
        """Связать метку («fig:scheme», «tbl:stages», «eq:rho», «app:form») с
        номером рисунка, таблицы, формулы или буквой приложения."""
        self._labels[label.lower()] = str(number)

    def label_number(self, label: str) -> Optional[str]:
        return self._labels.get(label.lower())

    def deferred_ref(self, label: str) -> str:
        """Номер по метке. Если элемент ещё не вставлен (ссылка вперёд, как
        требуют положения: «таблица 1» до самой таблицы) — метка-заглушка,
        которую r.save() заменит номером."""
        known = self.label_number(label)
        return known if known is not None else f"\u27e6{label}\u27e7"

    def _resolve_refs(self) -> None:
        missing = set()
        for t in self._doc.element.body.iter(qn("w:t")):
            text = t.text or ""
            if "\u27e6" not in text:
                continue

            def repl(m):
                value = self._labels.get(m.group(1).lower())
                if value is None:
                    missing.add(m.group(1))
                    return m.group(0)
                return value
            t.text = self._REF_TOKEN_RE.sub(repl, text)
        if missing:
            raise ValueError(
                "Ссылки на неизвестные метки: " + ", ".join(sorted(missing))
                + ". Метка ставится в label= у r.figure/r.table/r.formula/"
                  "r.appendix или {#метка} в Markdown.")

    def markdown(self, text: str, *, base_dir: Optional[Union[str, Path]] = None) -> None:
        """Текст работы в Markdown: заголовки, абзацы, списки, таблицы,
        рисунки, формулы, примечания, команды \\toc, \\appendix{…} и др.
        Оформление — из профиля. Синтаксис: scripts/markdown_input.py,
        references/api.md («Markdown»)."""
        import markdown_input
        markdown_input.render(self, text,
                              base_dir=Path(base_dir) if base_dir else None)

    def include(self, path: Union[str, Path]) -> None:
        """r.markdown(содержимое файла). Относительный путь — от build.py,
        затем от корня проекта и docs/. Картинки — от каталога файла."""
        p = Path(path).expanduser()
        if not p.is_absolute():
            caller = _caller_file()
            bases = ([caller.parent] if caller else []) + [
                self._paths.root, self._paths.docs, Path.cwd()]
            found = next((b / p for b in bases if (b / p).is_file()), None)
            if found is None:
                raise FileNotFoundError(
                    f"r.include: {path!r} не найден (искал от: "
                    + ", ".join(str(b) for b in bases) + ")")
            p = found
        self.markdown(p.read_text(encoding="utf-8"), base_dir=p.parent)

    def toc(self):
        """Вставляет автоматическое оглавление (поле Word TOC). Заголовок
        — `profile.toc_title` (по умолчанию «СОДЕРЖАНИЕ» в GOST_PROFILE,
        «ОГЛАВЛЕНИЕ» в ITMO_PROFILE).

        Также ставит флаг `updateFields=true` в settings.xml — Word/Pages
        при первом открытии файла предложит обновить поля (один клик
        «Yes» в диалоге), без ручного ПКМ по полю TOC.
        """
        self._enable_update_fields_on_open()
        after = _space_pt(self._profile.heading_space_after_h1, self._line_pt)
        title_p = self._add_paragraph(
            self._profile.toc_title,
            size=Pt(self._profile.heading_size_h1),
            bold=True,
            space_after=Pt(self._profile.heading_space_after_pt
                           if after is None else after),
        )
        # Оглавление после реферата / аннотации — тоже с новой страницы.
        if self._profile.h1_new_page and not self._just_broke_page:
            title_p.paragraph_format.page_break_before = True

        p = self._doc.add_paragraph()
        run = p.add_run()
        self._font_run(run, size=self._body_size)
        _append_word_field(
            run,
            ' TOC \\o "1-3" \\h \\z \\u ',
            placeholder=("Оглавление будет сформировано автоматически "
                         "при обновлении поля (ПКМ → «Обновить поле»)"),
            preserve_space=True,
        )

        self.page_break()

    # ---- заголовки ----
    _NUMBERED_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[А-ЯЁA-Z]\.\d+(?:\.\d+)*)\s")
    _CHAPTER_RE = re.compile(r"^\s*(?:(?:глава|раздел)\s+)?(\d+)(?=[\s.]|$)",
                             re.IGNORECASE)

    def _is_structural(self, text: str) -> bool:
        """Структурный элемент из profile.unnumbered_headings («ВВЕДЕНИЕ», …):
        не нумеруется. «*» в конце имени — совпадение по началу."""
        key = " ".join(text.split()).upper().rstrip(".")
        for name in self._profile.unnumbered_headings:
            name = name.upper().strip()
            if name.endswith("*"):
                if key.startswith(name[:-1].rstrip()):
                    return True
            elif key == name:
                return True
        return False

    def _heading_number(self, level: int, text: str,
                        numbered: Optional[bool]) -> Optional[str]:
        """Номер «1», «1.1», «1.1.1» при heading_numbering (или numbered=True).
        None — заголовок без номера."""
        nums = self._heading_numbers
        if level == 1:
            structural = self._is_structural(text)
            manual = bool(self._NUMBERED_RE.match(text))
            auto = numbered if numbered is not None else (
                self._profile.heading_numbering and not structural)
            self._in_chapter = bool(auto and not manual) or (manual and not structural)
            if not auto or manual:
                return None
            nums[0] += 1
            nums[1] = nums[2] = 0
            return str(nums[0])
        if numbered is False or self._NUMBERED_RE.match(text):
            return None
        if numbered is None and not (self._profile.heading_numbering
                                     and getattr(self, "_in_chapter", False)
                                     and nums[0] and not self._is_structural(text)):
            return None
        nums[level - 1] += 1
        for i in range(level, 3):
            nums[i] = 0
        return ".".join(str(n) for n in nums[:level])

    def _unlisted_style(self, level: int):
        """Стиль «как Heading N», но вне оглавления: уровень структуры
        «основной текст» (w:outlineLvl 9), TOC \\o и \\u его не берут."""
        from docx.enum.style import WD_STYLE_TYPE
        name = f"Heading {level} Unlisted"
        styles = self._doc.styles
        try:
            return styles[name]
        except KeyError:
            pass
        st = styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        st.base_style = styles[f"Heading {level}"]
        st.next_paragraph_style = styles["Normal"]
        ppr = st.element.get_or_add_pPr()
        lvl = OxmlElement("w:outlineLvl")
        lvl.set(qn("w:val"), "9")
        ppr.insert_element_before(lvl, "w:divId", "w:cnfStyle", "w:rPr",
                                  "w:sectPr", "w:pPrChange")
        return st

    def _heading(self, level: int, text: str, numbered: Optional[bool],
                 toc: bool = True):
        prof = self._profile
        p = self._doc.add_paragraph(
            style=f"Heading {level}" if toc else self._unlisted_style(level))
        if level == 1:
            # «С новой страницы» — свойство самого заголовка, не абзац с разрывом:
            # если предыдущая страница заполнена до конца, такой абзац уходит на
            # следующую, а заголовок ещё на одну, и между ними пустая страница.
            if prof.h1_new_page and not self._just_broke_page:
                p.paragraph_format.page_break_before = True
        number = self._heading_number(level, text, numbered)
        text = self._clean_display(text, glue=prof.heading_glue)
        if level == 1:
            text_to_render = text.upper() if prof.h1_uppercase else text
        else:
            text_to_render = text
        if number:
            text_to_render = f"{number}{prof.heading_number_sep}{text_to_render}"
        run = self._text_run(p, text_to_render,
                             size=Pt(getattr(prof, f"heading_size_h{level}")),
                             bold=getattr(prof, f"heading_bold_h{level}"),
                             italic=getattr(prof, f"heading_italic_h{level}"))
        # Два заголовка подряд: интервал между ними один, а не сумма
        # «после» первого и «до» второго (пропуск одной строки по ГОСТ).
        if (self._prev_kind == "heading"
                and getattr(prof, f"heading_space_before_h{level}") is not None):
            p.paragraph_format.space_before = Pt(0)
        if level == 1:
            self._appendix = None
            if number:
                chapter = number
            elif self._is_structural(text):
                chapter = None
            else:
                m = self._CHAPTER_RE.match(text)
                chapter = m.group(1) if m else None
            self._chapter = chapter
            self._reset_counters(chapter_only=True)
        self._prev_kind = "heading"
        self._prev_heading = p
        return p, run, text

    def h1(self, text: str, *, numbered: Optional[bool] = None,
           toc: bool = True):
        """Заголовок 1 уровня. Поведение управляется профилем:
        h1_new_page (новая страница), h1_uppercase (ВЕРХНИЙ регистр),
        heading_numbering (номер «1»; numbered=False/True — вручную).
        toc=False — без строки в оглавлении (реферат, аннотация перед ним).
        """
        p, run, text = self._heading(1, text, numbered, toc)
        if self._typography:
            if self._profile.h1_uppercase:
                # Прописным нужна разрядка 5% кегля. w:spacing в двадцатых долях пункта.
                spacing = OxmlElement("w:spacing")
                spacing.set(qn("w:val"), str(self._profile.heading_size_h1))
                run._element.get_or_add_rPr().insert_element_before(
                    spacing, "w:w", "w:kern", "w:position", "w:sz", "w:szCs",
                    "w:highlight", "w:u", "w:effect", "w:bdr", "w:shd",
                    "w:fitText", "w:vertAlign", "w:rtl", "w:cs", "w:em",
                    "w:lang", "w:eastAsianLayout", "w:specVanish", "w:oMath",
                )
            # Заголовок становится названием PDF.
            if not self._doc.core_properties.title:
                self._doc.core_properties.title = " ".join(text.split())
        self._just_broke_page = False

    def subtitle(self, text: str, *, italic: Optional[bool] = None,
                 bold: Optional[bool] = None,
                 line_spacing: Optional[float] = None,
                 space_after: Optional[Union[float, str]] = None):
        """Подзаголовок под h1: по центру, без переносов, не отрывается от
        следующего абзаца. По умолчанию курсив с одинарным интервалом; профиль
        (subtitle_*) и аргументы меняют начертание, интервал и отбивку после.
        subtitle_attach: интервал после заголовка уходит под подзаголовок.
        Перевод строки в тексте разбивает его по смыслу."""
        prof = self._profile
        italic = prof.subtitle_italic if italic is None else italic
        bold = prof.subtitle_bold if bold is None else bold
        ls = prof.subtitle_line_spacing if line_spacing is None else line_spacing
        after = self.space(prof.subtitle_space_after if space_after is None
                           else space_after) or Pt(0)
        if (prof.subtitle_attach and self._prev_kind == "heading"
                and self._prev_heading is not None):
            hpf = self._prev_heading.paragraph_format
            gap = hpf.space_after
            if gap is None:
                gap = self._prev_heading.style.paragraph_format.space_after or Pt(0)
            hpf.space_after = Pt(0)
            after = max(after, gap)
        p = self.add_inline_paragraph(text, italic=italic, bold=bold,
                                      line_spacing=ls, space_after=after)
        p.paragraph_format.keep_with_next = True
        _suppress_hyphens(p._p.get_or_add_pPr())
        self._prev_kind = "subtitle"
        return p

    def h2(self, text: str, *, numbered: Optional[bool] = None,
           toc: bool = True):
        self._heading(2, text, numbered, toc)

    def h3(self, text: str, *, numbered: Optional[bool] = None,
           toc: bool = True):
        self._heading(3, text, numbered, toc)

    # ---- проза ----
    def text(self, text: str, *, bold=False, italic=False,
             markup: Optional[bool] = None, lead: Optional[str] = None,
             indent: bool = True, align: Optional[str] = None,
             keep_with_next: bool = False):
        """Абзац основного текста: по ширине, с абзацного отступа. Поддерживает
        инлайновую математику $...$ (LaTeX → нативное уравнение).

        markup=True — **жирный** и *курсив* внутри текста (по умолчанию как в
        профиле: markup / typography). lead="Тема." — жирное начало абзаца
        («**Тема.** Автоматизация…»). indent=False — без абзацного отступа;
        align="left"/"center"/"right"/"justify". keep_with_next=True — абзац
        не остаётся последним на странице, а уходит вместе со следующим блоком
        (строка-заголовок перед таблицей: запрет висячего заголовка)."""
        p = self._make_paragraph(
            align=_align_const(align) if align else WD_ALIGN_PARAGRAPH.JUSTIFY,
            first_line_indent=self._first_line_indent if indent else Pt(0),
        )
        if lead:
            run = p.add_run(self._clean(lead.rstrip()) + " ")
            self._font_run(run, size=self._text_size(), bold=True, italic=italic)
        self._append_inline(p, text, bold=bold, italic=italic, markup=markup)
        if keep_with_next:
            p.paragraph_format.keep_with_next = True
        self._prev_kind = "text"
        return p

    def task(self, text: str):
        self.add_inline_paragraph(
            text,
            align=WD_ALIGN_PARAGRAPH.JUSTIFY,
            first_line_indent=self._first_line_indent,
            bold=True,
            space_before=self._space_block, space_after=self._space_block,
        )
        self._prev_kind = "text"

    def blank(self, count: int = 1) -> None:
        """Пустые строки основного текста (count штук)."""
        for _ in range(count):
            self._add_paragraph()
        self._prev_kind = "blank"

    def code(self, code: str):
        prof = self._profile
        for line in code.split("\n"):
            p = self._make_paragraph(
                align=WD_ALIGN_PARAGRAPH.LEFT,
                line_spacing=LINE_SPACING_CODE,
                first_line_indent=Pt(0),
                left_indent=Cm(prof.code_indent_cm),
            )
            run = p.add_run(line if line else " ")
            run.font.name = prof.code_font_name
            run.font.size = Pt(prof.code_font_size)
            _force_run_fonts(run._element.get_or_add_rPr(), prof.code_font_name)
        self._prev_kind = "code"

    def _resolve_figure_path(self, image_path: Union[str, Path]) -> Path:
        """Абсолютный путь — без изменений; относительный — от
        `self._paths.figures`. Бросает FileNotFoundError с обоими путями
        (input + resolved) если файл не существует.

        BC note: ранее относительные пути резолвились от os.getcwd(); теперь
        от <project>/docs/figures/. Реальные user-скрипты передавали
        абсолютные пути через Path(__file__).parent, поэтому wild-impact
        близок к нулю. Подробнее — CHANGELOG v0.7.0.
        """
        p = Path(image_path)
        if p.is_absolute():
            resolved = p
        else:
            resolved = self._paths.figures / p
        if not resolved.exists():
            raise FileNotFoundError(
                f"figure not found: input={image_path!r}, resolved={resolved}"
            )
        return resolved

    def figure(self, image, caption: str, *,
               width_cm: Optional[float] = None,
               explication: Optional[str] = None,
               label: Optional[str] = None):
        """Вставляет рисунок с автоматической подписью.

        `image` — путь к PNG/JPEG (str/Path) ИЛИ Figure-объект из подключаемого
        модуля (r.plot.line(...), r.diagram(...)). Все варианты делят ОДИН
        счётчик «Рисунок N — …» (ГОСТ 7.32 §6.5); по главам и в приложениях
        номер составной («1.2», «А.1», profile.numbering).

        Ширина картинки **всегда** ограничивается печатной областью страницы
        (A4 минус левое и правое поля активного профиля — обычно ~17 см для
        ITMO и ~16.5 см для GOST). Если width_cm не задан, используется
        натуральный размер картинки (с клампом к печатной области, если она
        больше). Если width_cm задан — он уважается, но также клампится.
        Это предотвращает вылет крупных скриншотов за поля.

        explication — пояснения к рисунку между изображением и подписью
        («1 – жилой корпус; 2 – ресторан»), по центру, кеглем подписи.

        Резолв пути:
        - абсолютный путь — без изменений.
        - относительный — резолвится от `<project>/docs/figures/`.
          `r.figure("load.png", ...)` → `<project>/docs/figures/load.png`.
          `r.figure("subdir/load.png", ...)` — тоже от `figures/`.
        Если файл не найден — FileNotFoundError с обоими путями (input + resolved).
        """
        from core_api import is_figure_like
        prof = self._profile
        if is_figure_like(image):
            image_path = Path(image.render(
                dpi=300, max_width_cm=self._printable_cm()))
        else:
            image_path = self._resolve_figure_path(image)
        number = self._next_number("figure")

        max_width_cm = self._printable_cm()

        image_path_str = str(image_path)
        if width_cm is None:
            # Прочитать натуральные размеры через python-docx (без PIL-зависимости)
            from docx.image.image import Image as _DocxImage
            img_meta = _DocxImage.from_file(image_path_str)
            natural_width_cm = img_meta.px_width / img_meta.horz_dpi * 2.54
            if natural_width_cm > max_width_cm:
                width_cm = max_width_cm
            # else: натуральный размер влезает, оставляем width_cm=None
        elif width_cm > max_width_cm:
            width_cm = max_width_cm

        p = self._make_paragraph(
            align=WD_ALIGN_PARAGRAPH.CENTER,
            first_line_indent=Pt(0),
            space_before=self._space_block,
        )
        run = p.add_run()
        if width_cm is not None:
            run.add_picture(image_path_str, width=Cm(width_cm))
        else:
            run.add_picture(image_path_str)
        if prof.caption_keep_with_next:
            p.paragraph_format.keep_with_next = True

        if explication:
            ep = self._make_paragraph(
                align=WD_ALIGN_PARAGRAPH.CENTER, first_line_indent=Pt(0),
                line_spacing=prof.caption_line_spacing)
            self._append_inline(ep, explication,
                                size=self._size_or_text(prof.caption_font_size))
            ep.paragraph_format.keep_with_next = True
            if not prof.caption_hyphenation:
                _suppress_hyphens(ep._p.get_or_add_pPr())

        self._add_caption(
            self._caption_prefix(prof.figure_label, number), caption,
            align=_align_const(prof.figure_caption_align),
            space_after=self._space_block,
        )
        if label:
            self.set_label(label, number)
        self._prev_kind = "figure"
        return number

    def formula(self, latex: str, *, where=None, label: Optional[str] = None):
        """Вставляет формулу из LaTeX как нативное Word-уравнение (OMML).

        Возвращает номер формулы — пригодится для ссылок:
            f1 = r.formula(r"E = mc^2")
            r.text(f"По формуле ({f1}) видно ...")

        Параметр `where` — пояснения переменных, идут отдельным абзацем
        под формулой:
            r.formula(
                r"\\frac{a + b}{c}",
                where="a, b — слагаемые, c — делитель",
            )
        или списком пар — каждый символ с новой строки (ГОСТ 7.32, 2.105):
            r.formula(r"\\rho = \\frac{m}{V}",
                      where=[("m", "масса образца, кг"), ("V", "объём образца, м³")])

        Сама строка `latex` не санируется (LaTeX-синтаксис не должен
        искажаться); `where` проходит как обычная проза. Символ в паре
        рендерится инлайн-формулой.

        Форматирование по ГОСТ 7.32: формула центрирована, номер «(N)»
        прижат к правому краю печатной области на той же строке.

        BC-shim: реализация вынесена в модуль gost_report_math (r.math.formula).
        label — метка для ссылок вперёд (r.ref.by_formula("eq:x")).
        """
        number = self.math.formula(latex, where=where)
        if label:
            self.set_label(label, number)
        return number

    # ---- таблицы ----
    _COL_ALIGN = {"l": "left", "c": "center", "r": "right", "j": "justify"}

    def _column_aligns(self, align, n_cols: int) -> List[Optional[str]]:
        if align is None:
            return [None] * n_cols
        if isinstance(align, str):
            align = [align] * n_cols if len(align) <= 1 or align in _ALIGN else list(align)
        out = []
        for j in range(n_cols):
            a = align[j] if j < len(align) else None
            a = self._COL_ALIGN.get(a, a) if a else None
            if a and a not in _ALIGN:
                raise ValueError(f"r.table: выравнивание столбца {a!r}: ожидается "
                                 "left/center/right/justify или l/c/r/j")
            out.append(a)
        return out

    def _set_table_widths(self, table, widths: Sequence[float]) -> None:
        """Ширины столбцов, см. Сумма больше печатной ширины — пропорционально
        ужимаются до неё. Раскладка фиксированная: Word не пересчитывает."""
        widths = [float(w) for w in widths]
        if len(widths) != len(table.columns):
            raise ValueError(f"r.table: widths на {len(widths)} столбцов, "
                             f"в таблице {len(table.columns)}")
        total = sum(widths)
        limit = self._printable_cm()
        if total > limit + 0.01:
            widths = [w * limit / total for w in widths]
        table.autofit = False              # python-docx ставит w:tblLayout fixed
        tblPr = table._tbl.tblPr
        tblW = tblPr.find(qn("w:tblW"))
        if tblW is not None:
            tblW.set(qn("w:type"), "dxa")
            tblW.set(qn("w:w"), str(int(round(sum(widths) / 2.54 * 1440))))
        for j, w in enumerate(widths):
            table.columns[j].width = Cm(w)
            for cell in table.columns[j].cells:
                cell.width = Cm(w)

    @staticmethod
    def _row_flag(row, tag: str) -> None:
        trPr = row._tr.get_or_add_trPr()
        if trPr.find(qn(tag)) is None:
            trPr.append(OxmlElement(tag))

    def table(self, rows: Sequence[Sequence[str]], caption: str = "",
              *, has_header: bool = True,
              widths: Optional[Sequence[float]] = None,
              align=None, header_align: Optional[str] = None,
              font_size: Optional[float] = None,
              repeat_header: Optional[bool] = None,
              cant_split: Optional[bool] = None,
              keep_together: Optional[bool] = None,
              note=None, continuation: Optional[bool] = None,
              label: Optional[str] = None):
        """Таблица + подпись «Таблица N — caption». Возвращает номер.

        widths — ширины столбцов в см; align — выравнивание всех столбцов
        ("center") или по столбцам (["left", "c", "r"]); header_align — шапки;
        font_size — кегль ячеек (иначе profile.table_font_size). repeat_header —
        шапка на каждой странице; cant_split — строки не рвутся; keep_together —
        таблица не делится между страницами; continuation — «Продолжение
        таблицы N» над второй частью (вёрстка LibreOffice в r.save()). None —
        как в профиле (table_*). note — примечание (строка или список) в
        последней строке таблицы, над нижней линией."""
        if not rows:
            return
        prof = self._profile
        number = self._next_number("table")

        if caption:
            cap = self._add_caption(
                self._caption_prefix(prof.table_label, number), caption,
                align=_align_const(prof.table_caption_align),
                space_before=self._space_block,
                first_line_indent=(Cm(prof.table_caption_indent_cm)
                                   if prof.table_caption_indent_cm else Pt(0)),
            )
            if prof.caption_keep_with_next:
                cap.paragraph_format.keep_with_next = True

        n_cols = max(len(r) for r in rows)
        table = self._doc.add_table(rows=len(rows), cols=n_cols)
        table.style = "Table Grid"
        size = Pt(font_size) if font_size else self._size_or_text(prof.table_font_size)
        col_aligns = self._column_aligns(align, n_cols)
        head_align = header_align or prof.table_header_align or None
        for i, row_data in enumerate(rows):
            is_head = has_header and i == 0
            for j in range(n_cols):
                cell = table.rows[i].cells[j]
                cell.text = ""
                p = cell.paragraphs[0]
                p.paragraph_format.line_spacing = prof.table_line_spacing
                p.paragraph_format.first_line_indent = Pt(0)
                a = (head_align or col_aligns[j]) if is_head else col_aligns[j]
                if a:
                    p.alignment = _align_const(a)
                value = row_data[j] if j < len(row_data) else ""
                # _append_inline санирует прозовые сегменты ячейки,
                # корректно ПРОПУСКАЯ $...$ (LaTeX/математику).
                # Санировать здесь весь raw value нельзя — это искажало бы тире
                # внутри инлайновой математики в ячейках таблицы.
                self._append_inline(p, value, size=size,
                                    bold=(is_head and prof.table_header_bold))
        if widths:
            self._set_table_widths(table, widths)
        if note:
            self._table_note(table, note, size)
        repeat = prof.table_repeat_header if repeat_header is None else repeat_header
        if has_header and repeat:
            self._row_flag(table.rows[0], "w:tblHeader")
        if prof.table_cant_split if cant_split is None else cant_split:
            for row in table.rows:
                self._row_flag(row, "w:cantSplit")
        if prof.table_keep_together if keep_together is None else keep_together:
            for row in list(table.rows)[:-1]:
                for cell in row.cells:
                    for cp in cell.paragraphs:
                        cp.paragraph_format.keep_with_next = True
        if caption and (prof.table_continuation if continuation is None else continuation):
            self._continuation.append((table._tbl, str(number),
                                       1 if has_header else 0))
        if label:
            self.set_label(label, number)
        self._prev_kind = "table"
        return number

    def _table_note(self, table, note, size) -> None:
        """Примечание к таблице: объединённая последняя строка внутри рамки."""
        prof = self._profile
        row = table.add_row()
        cell = row.cells[0].merge(row.cells[-1]) if len(row.cells) > 1 else row.cells[0]
        items = [note] if isinstance(note, str) else list(note)
        lines = ([f"{prof.note_label} {prof.dash} {items[0]}"] if len(items) == 1
                 else [f"{prof.notes_label}:"] + [f"{i}. {t}" for i, t in enumerate(items, 1)])
        grid = table._tbl.find(qn("w:tblGrid"))
        if table._tbl.tblPr.find(qn("w:tblLayout")) is not None and grid is not None:
            from docx.shared import Twips
            cell.width = Twips(sum(int(gc.get(qn("w:w"))) for gc in grid.findall(qn("w:gridCol"))))
        for k, line in enumerate(lines):
            p = cell.paragraphs[0] if k == 0 else cell.add_paragraph()
            if k == 0:
                for r in list(p.runs):
                    r._r.getparent().remove(r._r)
            p.paragraph_format.line_spacing = prof.table_line_spacing
            p.paragraph_format.first_line_indent = self._first_line_indent
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            self._append_inline(p, line, size=size)

    # ---- перечисления ----
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
        # align и space_before должны прийти из стиля списка
        # (`List Number` / `List Bullet`). Без _INHERIT мы бы навязали
        # alignment=CENTER и w:before="0" в каждом list-параграфе.
        p = self._make_paragraph(
            align=_INHERIT,
            space_before=_INHERIT,
        )

        pPr = p._p.get_or_add_pPr()
        numPr = OxmlElement("w:numPr")
        ilvl = OxmlElement("w:ilvl")
        ilvl.set(qn("w:val"), "0")
        numId_el = OxmlElement("w:numId")
        numId_el.set(qn("w:val"), str(num_id))
        numPr.append(ilvl)
        numPr.append(numId_el)
        pPr.append(numPr)

        self._append_inline(p, text)

    def _ensure_numbering_part(self, style_name: str) -> None:
        """Триггерит создание numbering.xml части документа, если её ещё нет.
        python-docx создаёт numbering_part лениво, при первом использовании
        стиля списка. Бросаем туда-сюда пустой параграф со стилем чтобы
        гарантировать наличие numbering_part до _find_abstract_num_for_style."""
        try:
            _ = self._doc.part.numbering_part
        except (AttributeError, KeyError):
            tmp = self._doc.add_paragraph(style=style_name)
            tmp._element.getparent().remove(tmp._element)

    def _list(self, items, *, style_name: str) -> None:
        if isinstance(items, str):
            items = [items]
        if not items:
            return
        self._ensure_numbering_part(style_name)
        abstract_id = self._find_abstract_num_for_style(style_name)
        if abstract_id is None:
            for item in items:
                p = self._doc.add_paragraph(style=style_name)
                pf = p.paragraph_format
                pf.line_spacing = self._line_spacing
                pf.space_after = Pt(0)
                run = p.add_run(self._clean(item))
                self._font_run(run, size=self._body_size)
            return
        num_id = self._create_independent_num(abstract_id)
        for item in items:
            self._add_list_paragraph(item, num_id)

    def _custom_lists(self, items) -> bool:
        """Нужен ли новый движок списков: профиль меняет маркер/формат/раскладку
        или в пунктах есть вложенность. Иначе — прежний путь байт-в-байт."""
        p = self._profile
        if p.list_layout != "hanging" or p.bullet_char or p.numbered_format:
            return True
        if isinstance(items, str):
            return False
        return any(isinstance(it, tuple) for it in items)

    def _list_abstract(self, kind: str) -> str:
        """abstractNum под вид списка и раскладку профиля (создаётся один раз)."""
        if kind in self._abstract_ids:
            return self._abstract_ids[kind]
        prof = self._profile
        self._ensure_numbering_part("List Number")
        numbering = self._doc.part.numbering_part.element
        abs_id = str(60 + len(self._abstract_ids))
        an = OxmlElement("w:abstractNum")
        an.set(qn("w:abstractNumId"), abs_id)
        mlt = OxmlElement("w:multiLevelType")
        mlt.set(qn("w:val"), "singleLevel")
        an.append(mlt)
        lvl = OxmlElement("w:lvl")
        lvl.set(qn("w:ilvl"), "0")

        def child(tag, val):
            el = OxmlElement(tag)
            el.set(qn("w:val"), val)
            lvl.append(el)
            return el

        child("w:start", "1")
        if kind == "bullet":
            child("w:numFmt", "bullet")
        else:
            child("w:numFmt", "decimal")
        paragraph = prof.list_layout == "paragraph"
        if paragraph:
            child("w:suff", "space")
        if kind == "bullet":
            child("w:lvlText", prof.bullet_char or "•")
        else:
            child("w:lvlText", prof.numbered_format or "%1.")
        child("w:lvlJc", "left")
        ppr = OxmlElement("w:pPr")
        ind = OxmlElement("w:ind")
        if paragraph:
            ind.set(qn("w:left"), "0")
            ind.set(qn("w:firstLine"), str(int(round(prof.first_line_indent_cm / 2.54 * 1440))))
        else:
            tabs = OxmlElement("w:tabs")
            tab = OxmlElement("w:tab")
            tab.set(qn("w:val"), "num")
            tab.set(qn("w:pos"), "360")
            tabs.append(tab)
            ppr.append(tabs)
            ind.set(qn("w:left"), "360")
            ind.set(qn("w:hanging"), "360")
        ppr.append(ind)
        lvl.append(ppr)
        rpr = OxmlElement("w:rPr")
        rfonts = OxmlElement("w:rFonts")
        for attr in ("w:ascii", "w:hAnsi", "w:cs"):
            rfonts.set(qn(attr), self._font)
        rfonts.set(qn("w:hint"), "default")
        rpr.append(rfonts)
        lvl.append(rpr)
        an.append(lvl)
        first_num = numbering.find(qn("w:num"))
        if first_num is not None:
            first_num.addprevious(an)
        else:
            numbering.append(an)
        self._abstract_ids[kind] = abs_id
        return abs_id

    def _list_item(self, kind: str, marker: Optional[str], text: str,
                   num_id: Optional[int], level: int) -> None:
        prof = self._profile
        paragraph = prof.list_layout == "paragraph"
        size = self._text_size()
        extra = Cm(prof.list_nested_indent_cm * level)
        if paragraph:
            p = self._make_paragraph(
                align=WD_ALIGN_PARAGRAPH.JUSTIFY,
                first_line_indent=Cm(prof.first_line_indent_cm) + extra,
                left_indent=Cm(0))
        else:
            p = self._make_paragraph(
                align=WD_ALIGN_PARAGRAPH.LEFT if marker else _INHERIT,
                space_before=_INHERIT,
                left_indent=Cm(0.635 * (level + 1)) if (level or marker) else None,
                first_line_indent=Cm(-0.635) if (level or marker) else None)
        if num_id is not None:
            pPr = p._p.get_or_add_pPr()
            numPr = OxmlElement("w:numPr")
            ilvl = OxmlElement("w:ilvl")
            ilvl.set(qn("w:val"), "0")
            nid = OxmlElement("w:numId")
            nid.set(qn("w:val"), str(num_id))
            numPr.append(ilvl)
            numPr.append(nid)
            # По схеме w:pPr numPr стоит до spacing/ind/jc.
            pPr.insert_element_before(
                numPr, "w:suppressLineNumbers", "w:pBdr", "w:shd", "w:tabs",
                "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
                "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE",
                "w:autoSpaceDN", "w:bidi", "w:adjustRightInd", "w:snapToGrid",
                "w:spacing", "w:ind", "w:contextualSpacing", "w:mirrorIndents",
                "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment",
                "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle",
                "w:rPr", "w:sectPr", "w:pPrChange")
            if size != self._body_size:
                _mark_run_size(pPr, size)
        if marker:
            run = p.add_run(marker + (" " if paragraph else "\t"))
            self._font_run(run, size=size)
        self._append_inline(p, text, size=size)

    def _list_v2(self, items, kind: str, nested: str, level: int = 0) -> None:
        if isinstance(items, str):
            items = [items]
        items = list(items)
        if not items:
            return
        prof = self._profile
        num_id = None
        if kind in ("bullet", "numbered"):
            num_id = self._create_independent_num(self._list_abstract(kind))
        letters = prof.list_letters or "абвгдежиклмнпрстуфхцшщэюя"
        for idx, item in enumerate(items):
            if isinstance(item, tuple):
                text, sub = item[0], item[1] if len(item) > 1 else None
            else:
                text, sub = item, None
            marker = None
            if kind == "lettered":
                letter = letters[idx % len(letters)] * (idx // len(letters) + 1)
                marker = prof.lettered_format.format(letter=letter, n=idx + 1)
            self._list_item(kind, marker, text, num_id, level)
            if sub:
                self._list_v2(sub, nested, nested, level + 1)
        self._prev_kind = "list"

    def numbered(self, items, *, nested: str = "bullet"):
        """Нумерованный список «1.» (профиль: numbered_format "%1)" → «1)»).
        Пункт ("текст", [вложенные]) — вложенный список вида nested."""
        if self._custom_lists(items):
            return self._list_v2(items, "numbered", nested)
        self._list(items, style_name="List Number")
        self._prev_kind = "list"

    def bullet(self, items, *, nested: str = "numbered"):
        """Маркированный список (профиль: bullet_char "–" по ГОСТ).
        Пункт ("текст", [вложенные]) — вложенный список вида nested."""
        if self._custom_lists(items):
            return self._list_v2(items, "bullet", nested)
        self._list(items, style_name="List Bullet")
        self._prev_kind = "list"

    def lettered(self, items, *, nested: str = "numbered"):
        """Перечисление «а)», «б)», … без ё, з, й, о, ч, ъ, ы, ь (profile.list_letters)
        — для пунктов, на которые ссылаются в тексте. Пункт ("текст",
        ["вложенный", …]) даёт вложенный «1)» (nested="numbered")."""
        return self._list_v2(items, "lettered", nested)

    # ---- реферат, примечания, приложения ----
    def _ensure_paragraph_style(self, name: str):
        from docx.enum.style import WD_STYLE_TYPE
        styles = self._doc.styles
        try:
            return styles[name]
        except KeyError:
            st = styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
            st.base_style = styles["Normal"]
            return st

    def keywords(self, words, *, uppercase: bool = True):
        """Ключевые слова реферата: прописными, в строку через запятые, без
        абзацного отступа, без переносов и без точки в конце. words — список
        или строка через запятые. Число вне keywords_min…keywords_max профиля
        (5–15) — предупреждение в stderr и WARN валидатора."""
        if isinstance(words, str):
            items = [w.strip() for w in words.split(",")]
        else:
            items = [str(w).strip() for w in words]
        items = [w.rstrip(".").strip() for w in items if w.strip()]
        prof = self._profile
        if not (prof.keywords_min <= len(items) <= prof.keywords_max):
            sys.stderr.write(
                f"gost-report warn: ключевых слов {len(items)}, нужно "
                f"{prof.keywords_min}–{prof.keywords_max}.\n")
        text = ", ".join(items)
        if uppercase:
            text = text.upper()
        # «Без переноса слов» (прил. 9 МБИ): «БИЗНЕС-ПРОЦЕСС» не рвётся на дефисе.
        text = re.sub(r"(?<=\w)-(?=\w)", NBHY, text)
        p = self._make_paragraph(align=_align_const(prof.keywords_align),
                                 first_line_indent=Pt(0))
        p.style = self._ensure_paragraph_style("Keywords")
        self._append_inline(p, text, markup=False)
        _suppress_hyphens(p._p.get_or_add_pPr())
        self._prev_kind = "text"
        return p

    def volume_line(self, template: Optional[str] = None, **values):
        """Строка объёма реферата: «… содержит {pages} с., {figures} рис.,
        {tables} табл., {sources} {sources_word}, {appendices} прил.»
        (profile.volume_template). Числа подставляются в r.save(): рисунки,
        таблицы, источники r.bib, приложения — по документу, {pages} — поле
        Word NUMPAGES (результат считает вёрстка LibreOffice, если она есть).
        values перекрывают любое число: pages=58."""
        tpl = template or self._profile.volume_template
        p = self._make_paragraph(align=WD_ALIGN_PARAGRAPH.JUSTIFY,
                                 first_line_indent=self._first_line_indent)
        size = self._text_size()
        slots = []
        for part in re.split(r"(\{[a-z_]+\})", tpl):
            if not part:
                continue
            m = re.fullmatch(r"\{([a-z_]+)\}", part)
            if not m:
                run = p.add_run(self._clean(part))
                self._font_run(run, size=size)
                continue
            key = m.group(1)
            if key == "pages" and "pages" not in values:
                self._enable_update_fields_on_open()
                slots.append(("pages", self._add_field_runs(p, "NUMPAGES", "0", size)))
                continue
            run = p.add_run("0")
            self._font_run(run, size=size)
            slots.append((key, run))
        self._volume_slots.append((slots, dict(values)))
        self._prev_kind = "text"
        return p

    def _add_field_runs(self, p, instr: str, result: str, size):
        """Поле Word отдельными run'ами (begin / instr / separate / result / end):
        в одном run LibreOffice теряет форматирование. Возвращает run результата."""
        def mk(el=None, text=None):
            run = p.add_run(text) if text is not None else p.add_run()
            self._font_run(run, size=size)
            if el is not None:
                run._element.append(el)
            return run
        begin = OxmlElement("w:fldChar")
        begin.set(qn("w:fldCharType"), "begin")
        mk(begin)
        it = OxmlElement("w:instrText")
        it.set(qn("xml:space"), "preserve")
        it.text = f" {instr} "
        mk(it)
        sep = OxmlElement("w:fldChar")
        sep.set(qn("w:fldCharType"), "separate")
        mk(sep)
        result_run = mk(text=result)
        end = OxmlElement("w:fldChar")
        end.set(qn("w:fldCharType"), "end")
        mk(end)
        return result_run

    def note(self, text, *, label: Optional[str] = None, example: bool = False):
        """Примечание по ГОСТ: одно — «Примечание – Текст» с абзацного отступа;
        несколько (список) — «Примечания:», затем «1. Текст», «2. Текст».
        Метки и тире — из профиля (note_label, notes_label, dash)."""
        prof = self._profile
        items = [text] if isinstance(text, str) else list(text)
        size = self._size_or_text(prof.note_font_size)
        if len(items) == 1:
            lab = label or (prof.example_label if example else prof.note_label)
            p = self._make_paragraph(align=WD_ALIGN_PARAGRAPH.JUSTIFY,
                                     first_line_indent=self._first_line_indent)
            run = p.add_run(f"{lab} {prof.dash} ")
            self._font_run(run, size=size)
            self._append_inline(p, items[0], size=size)
        else:
            lab = label or (prof.examples_label if example else prof.notes_label)
            self._add_paragraph(f"{lab}:", align=WD_ALIGN_PARAGRAPH.JUSTIFY,
                                first_line_indent=self._first_line_indent, size=size)
            for i, item in enumerate(items, 1):
                q = self._make_paragraph(align=WD_ALIGN_PARAGRAPH.JUSTIFY,
                                         first_line_indent=self._first_line_indent)
                run = q.add_run(f"{i}. ")
                self._font_run(run, size=size)
                self._append_inline(q, item, size=size)
        self._prev_kind = "text"

    def example(self, text, *, label: Optional[str] = None):
        """Пример по ГОСТ: оформляется как примечание («Пример – …»)."""
        return self.note(text, label=label, example=True)

    def appendix(self, title: str, status: str = "обязательное", *,
                 letter: Optional[str] = None,
                 label: Optional[str] = None) -> str:
        """Приложение: с новой страницы, по центру «ПРИЛОЖЕНИЕ А», под ним
        «(обязательное)» / «(рекомендуемое)» / «(справочное)», затем заголовок
        с прописной. Буквы — profile.appendix_letters (без Ё, З, Й, О, Ч, Ъ,
        Ы, Ь). Рисунки, таблицы и формулы дальше нумеруются «А.1»; кегль
        текста — profile.appendix_font_size. Возвращает букву (для r.ref)."""
        prof = self._profile
        if letter is None:
            free = [ch for ch in prof.appendix_letters if ch not in self._appendices]
            if not free:
                raise ValueError("r.appendix: буквы profile.appendix_letters закончились")
            letter = free[0]
        self._appendices.append(letter)
        p = self._doc.add_paragraph(style="Heading 1")
        if not self._just_broke_page:
            p.paragraph_format.page_break_before = True
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.first_line_indent = Pt(0)
        size = Pt(prof.heading_size_h1)
        head = p.add_run(f"{prof.appendix_label} {letter}")
        self._font_run(head, size=size, bold=True)
        head.add_break()
        if status:
            st = p.add_run(f"({status})")
            self._font_run(st, size=size, bold=False)
            st.add_break()
        title = self._clean_display(title.strip(), glue=prof.heading_glue)
        title = title[:1].upper() + title[1:]
        tr = p.add_run(title)
        self._font_run(tr, size=size, bold=True)
        self._appendix = letter
        if label:
            self.set_label(label, letter)
        self._chapter = None
        self._reset_counters(chapter_only=False)
        self._just_broke_page = False
        self._prev_kind = "heading"
        self._prev_heading = p
        return letter

    def page_break(self):
        """Следующий блок начнётся с новой страницы.

        В готовом файле это свойство «с новой страницы» на следующем абзаце,
        а не абзац с разрывом: `save()` переносит его через
        `_resolve_page_breaks()`, потому что на момент вызова следующего
        блока ещё нет.
        """
        p = self._doc.add_paragraph()
        run = p.add_run()
        run.add_break(WD_BREAK.PAGE)
        self._break_paragraphs.append(p._p)
        self._just_broke_page = True

    def _resolve_page_breaks(self) -> None:
        """Заменяет абзацы-разрывы от `page_break()` на `pageBreakBefore`
        у следующего блока (у таблицы — в первом абзаце первой ячейки).

        Абзац с разрывом занимает строку: на заполненной до конца странице он
        уезжает на следующую и оставляет пустую. Разрыв, после которого ничего
        нет, остаётся как есть. Два разрыва подряд дают пустую страницу: первый
        абзац теряет `<w:br>` и остаётся пустым, с тем же свойством.
        """
        pending, self._break_paragraphs = self._break_paragraphs, []
        for br_p in pending:
            if br_p.getparent() is None:
                continue
            target = br_p.getnext()
            while target is not None and target.tag not in (qn("w:p"), qn("w:tbl")):
                target = target.getnext()
            if target is None:
                self._break_paragraphs.append(br_p)
                continue
            if target.tag == qn("w:tbl"):
                target = next(target.iter(qn("w:p")), None)
                if target is None:
                    self._break_paragraphs.append(br_p)
                    continue
            target.get_or_add_pPr().get_or_add_pageBreakBefore()
            if br_p.find(qn("w:pPr") + "/" + qn("w:pageBreakBefore")) is None:
                br_p.getparent().remove(br_p)
            else:
                for r in br_p.findall(qn("w:r")):
                    br_p.remove(r)

    def save(self, path: Optional[Union[str, Path]] = None, *,
             layout: Optional[bool] = None) -> Path:
        """Сохранить .docx. Если path не задан — `<project>/docs/report.docx`.
        Относительный путь — от `<project>/docs/`. Абсолютный — без изменений.
        Создаёт parent-директории при необходимости. Возвращает абсолютный Path.

        Проход вёрстки (LibreOffice, если установлен): «Продолжение таблицы N»
        для таблиц с continuation и число страниц в r.volume_line. Нужен только
        им; layout=False отключает, layout=True требует LibreOffice.

        После записи файл валидируется автоматически через `validate.py`.
        Если найдены нарушения уровня FAIL — поднимается `GostValidationError`.
        Тёплые предупреждения (heuristic) печатаются в stderr и не блокируют.
        Если validate.py недоступен (битый venv) — save() работает как раньше,
        отсутствие валидации лучше падения библиотеки.
        """
        if path is None:
            target = self._paths.out
        else:
            p = Path(path)
            target = p if p.is_absolute() else self._paths.docs / p
        target.parent.mkdir(parents=True, exist_ok=True)
        self._resolve_page_breaks()
        self._resolve_refs()
        pages_needed = self._fill_volume_lines()
        _strip_python_docx_fingerprints(self._doc)
        self._write(target)
        if (self._continuation or pages_needed) and layout is not False:
            import layout as _layout
            _layout.run_layout_pass(self, target, pages=pages_needed,
                                    required=bool(layout))
        target = target.resolve()
        self._saved_docx = target

        # Картинки уже встроены байтами в .docx — tmp-PNG модулей больше не нужны.
        self._cleanup()

        try:
            import validate as _validate
        except ImportError:
            sys.stderr.write(
                "gost-report: validate.py недоступен, проверка пропущена.\n"
            )
            return target

        try:
            profile_name = self._profile.name or type(self._profile).__name__
            for var_name, value in globals().items():
                if value is self._profile and var_name.endswith("_PROFILE"):
                    profile_name = var_name
                    break
            _validate.write_sentinel(target, profile_name=profile_name,
                                     allow_dashes=self._prose_mode == "keep",
                                     style=self._validation_style())
            violations = _validate.validate_docx(target)
        except Exception as e:
            sys.stderr.write(
                f"gost-report: валидатор упал ({e}); проверка пропущена.\n"
            )
            return target

        warns = [v for v in violations if v.tier == _validate.TIER_WARN]
        for v in warns:
            sys.stderr.write("gost-report warn: " + v.format().lstrip() + "\n")

        if _validate.has_failures(violations):
            raise GostValidationError(
                _validate.format_error(target, violations)
            )
        return target

    def _write(self, target: Path) -> None:
        """Запись .docx без отпечатков python-docx (thumbnail)."""
        self._doc.save(str(target))
        _strip_thumbnail_part(target)

    def _validation_style(self) -> Dict[str, Any]:
        """Правила профиля для validate.py: пишутся в sentinel, чтобы Stop-хук
        и `validate.py --check` проверяли документ по тем же правилам."""
        return validation_style(self._profile, prose_mode=self._prose_mode,
                                title_page=self._has_title_page)

    def _totals(self) -> Dict[str, int]:
        """Сколько в документе рисунков, таблиц, источников, приложений."""
        bib = (self.__dict__.get("_attached") or {}).get("bib")
        sources = getattr(bib, "count", 0) if bib is not None else 0
        return {"figures": self._kind_totals["figure"],
                "tables": self._kind_totals["table"],
                "formulas": self._kind_totals["formula"],
                "sources": sources, "appendices": len(self._appendices)}

    def _fill_volume_lines(self) -> bool:
        """Подставляет числа в r.volume_line. True — нужен счёт страниц вёрсткой."""
        if not self._volume_slots:
            return False
        totals = self._totals()
        need_pages = False
        for slots, values in self._volume_slots:
            nums = {**totals, **values}
            nums["sources_word"] = _plural(int(nums.get("sources", 0) or 0),
                                           "источник", "источника", "источников")
            for key, run in slots:
                if key == "pages" and "pages" not in values:
                    need_pages = True
                    self._pages_runs.append(run)
                    continue
                value = nums.get(key)
                if value is None:
                    raise ValueError(f"r.volume_line: неизвестное поле {{{key}}}")
                run.text = str(value)
        return need_pages

    def set_page_count(self, pages: int) -> None:
        """Результат полей NUMPAGES строки объёма (проход вёрстки)."""
        for run in self._pages_runs:
            run.text = str(pages)

    def export_pdf(self, path: Optional[Union[str, Path]] = None,
                   *, check: bool = True) -> Path:
        """PDF из сохранённого .docx через LibreOffice с русскими переносами.
        Без path кладёт файл рядом с .docx. check=True печатает в stderr дефекты
        вёрстки: висячие предлоги, дыры в строках, подмену шрифта (см. pdf_export)."""
        if self._saved_docx is None:
            raise RuntimeError("Сначала r.save(): export_pdf конвертирует сохранённый .docx.")
        import pdf_export
        pdf = pdf_export.export_pdf(self._saved_docx, path)
        if check:
            # nbsp="required" сознательно не клеит предлоги: висячий предлог
            # в таком стиле не дефект, а выбор положения.
            skip = ({"hanging-word"} if not self._typography
                    and self._profile.nbsp == "required" else set())
            for issue in pdf_export.check_pdf(pdf, font=self._font):
                if issue.code not in skip:
                    sys.stderr.write(f"gost-report pdf warn: {issue}\n")
        return pdf

    @property
    def doc(self) -> _Document:
        return self._doc

    @property
    def profile(self) -> UniversityProfile:
        return self._profile
