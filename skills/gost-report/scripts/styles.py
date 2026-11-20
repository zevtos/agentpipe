#!/usr/bin/env python3
"""styles — стилевые файлы gost-report: профиль вуза в TOML/JSON с наследованием.

Как класс документа в LaTeX: оформление (поля, кегли, заголовки, подписи,
списки, таблицы, титул, правила проверки) живёт в файле стиля, а build.py
только выбирает его:

    r = Report(TitleConfig(...), profile="mbi")              # встроенный стиль
    r = Report(TitleConfig(...), profile=".gost-report/vkr.toml")

Файл стиля:

    # .gost-report/styles/vkr.toml
    extends = "mbi"              # встроенный стиль, имя другого файла или путь
    numbering = "chapter"        # ключ верхнего уровня = поле UniversityProfile
    [h2]                         # секция = группа полей
    space_before = "1line"       # → heading_space_before_h2
    [table]
    font_size = 12               # → table_font_size

Секции: [page] (поля и номер страницы основного текста), [body], [h1] [h2] [h3],
[headings], [caption], [table], [list], [formula], [note], [keywords],
[subtitle], [code], [bib], [appendix], [toc], [title] (+ [[title.blocks]] —
блоки шаблонного титула). Ключ внутри секции без префикса: [table] font_size.
Неизвестный ключ — ошибка с подсказкой ближайших имён.

Поиск стиля по имени (первый найденный): каталог build.py и его
.gost-report/styles/, <проект>/.gost-report/styles/, ~/.config/gost-report/styles/,
встроенные <skill>/styles/*.toml, Python-пресеты gost и itmo.

CLI (без python-docx в текущем Python сам перезапускается в venv скилла):
    python3 styles.py list                 # доступные стили
    python3 styles.py fields [секция]      # все поля: тип, дефолт, описание
    python3 styles.py show mbi [--all]     # итоговые значения стиля (TOML)
    python3 styles.py check path.toml      # проверить файл стиля
"""
from __future__ import annotations

import dataclasses
import difflib
import json
import os
import sys
import typing
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

SCRIPTS_DIR = Path(__file__).resolve().parent
BUILTIN_DIR = SCRIPTS_DIR.parent / "styles"
STYLE_SUFFIXES = (".toml", ".json")

# Python-пресеты: объекты в gost_report, их байты закреплены тестами.
_PY_PRESETS = {"gost": "GOST_PROFILE", "itmo": "ITMO_PROFILE"}

# ---------------------------------------------------------------------------
# Описание полей: секция → поле → (короткое имя в секции, описание)
# ---------------------------------------------------------------------------
# Порядок секций = порядок в `styles.py fields`. Каждое поле UniversityProfile
# обязано быть здесь (проверяется в check_docs()).
FIELD_DOCS: Dict[str, Dict[str, Tuple[str, str]]] = {
    "meta": {
        "name": ("name", "имя стиля (заполняется по имени файла)"),
        "base_dir": ("base_dir", "каталог файла стиля, от него резолвится title_docx"),
    },
    "page": {
        "body_margin_left": ("margin_left", "левое поле основного текста, мм"),
        "body_margin_right": ("margin_right", "правое поле, мм"),
        "body_margin_top": ("margin_top", "верхнее поле, мм"),
        "body_margin_bottom": ("margin_bottom", "нижнее поле, мм"),
        "page_number_position": ("number_position",
                                 "номер страницы: <top|bottom>-<left|center|right>"),
        "page_number_size": ("number_size", "кегль номера страницы, пт"),
    },
    "body": {
        "font_name": ("font", "гарнитура всего документа"),
        "body_font_size": ("font_size", "кегль основного текста, пт"),
        "line_spacing": ("line_spacing", "межстрочный интервал, множитель"),
        "first_line_indent_cm": ("indent_cm", "абзацный отступ, см"),
        "block_space_pt": ("block_space_pt",
                           "интервал вокруг рисунков, подписей таблиц, заданий, пт"),
        "dash": ("dash", "тире в «Рисунок 1 — …», «Примечание — …», списке литературы"),
        "prose_dashes": ("prose_dashes",
                         "тире в прозе: strip (убрать, правило №1) | normalize "
                         "(« - »/« — » → « <dash> », «45–60» сохраняется) | keep"),
        "nbsp": ("nbsp", "неразрывные пробелы: off | required (инициалы, число+"
                         "единица, «г.», «№ 5», «п. 2») | full (вся типографика)"),
        "auto_hyphenation": ("hyphenation", "автоперенос (язык ru-RU), заголовки без переносов"),
        "widow_control": ("widow_control", "запрет висячих строк"),
        "language": ("language", "язык документа w:lang, например ru-RU"),
        "markup": ("markup", "**жирный** и *курсив* в r.text без режима typography"),
    },
    "headings": {
        "heading_numbering": ("numbering", "автономера «1», «1.1», «1.1.1» без точки"),
        "heading_number_sep": ("number_sep", "разделитель номера и текста заголовка"),
        "unnumbered_headings": ("unnumbered",
                                "структурные элементы без номера; «*» — по началу"),
        "heading_glue": ("glue", "предлоги и союзы не остаются в конце строки"),
        "heading_line_spacing": ("line_spacing", "интервал заголовков (по умолч. как текст)"),
        "heading_space_after_pt": ("space_after_pt",
                                   "интервал после заголовков и названия оглавления, пт"),
        "toc_title": ("toc_title", "заголовок оглавления: СОДЕРЖАНИЕ / ОГЛАВЛЕНИЕ"),
    },
    "h1": {
        "heading_size_h1": ("size", "кегль, пт"),
        "heading_align_h1": ("align", "center | left | right | justify"),
        "heading_indent_cm_h1": ("indent_cm", "абзацный отступ, см"),
        "heading_space_before_h1": ("space_before", "до: пт или «1line», «12pt»"),
        "heading_space_after_h1": ("space_after", "после: пт или «1line»"),
        "heading_bold_h1": ("bold", "полужирный"),
        "heading_italic_h1": ("italic", "курсив"),
        "h1_uppercase": ("uppercase", "ПРОПИСНЫМИ"),
        "h1_new_page": ("new_page", "с новой страницы"),
    },
    "h2": {
        "heading_size_h2": ("size", "кегль, пт"),
        "heading_align_h2": ("align", "center | left | right | justify"),
        "heading_indent_cm_h2": ("indent_cm", "абзацный отступ, см"),
        "heading_space_before_h2": ("space_before", "до: пт или «1line»"),
        "heading_space_after_h2": ("space_after", "после: пт или «1line»"),
        "heading_bold_h2": ("bold", "полужирный"),
        "heading_italic_h2": ("italic", "курсив"),
    },
    "h3": {
        "heading_size_h3": ("size", "кегль, пт"),
        "heading_align_h3": ("align", "center | left | right | justify"),
        "heading_indent_cm_h3": ("indent_cm", "абзацный отступ, см"),
        "heading_space_before_h3": ("space_before", "до: пт или «1line»"),
        "heading_space_after_h3": ("space_after", "после: пт или «1line»"),
        "heading_bold_h3": ("bold", "полужирный"),
        "heading_italic_h3": ("italic", "курсив"),
    },
    "caption": {
        "figure_label": ("figure_label", "слово перед номером рисунка"),
        "table_label": ("table_label", "слово перед номером таблицы"),
        "figure_caption_align": ("figure_align", "подпись рисунка: center | left | …"),
        "table_caption_align": ("table_align", "подпись таблицы: left | center | …"),
        "table_caption_indent_cm": ("table_indent_cm", "отступ подписи таблицы, см"),
        "caption_font_size": ("font_size", "кегль подписей (по умолч. как текст)"),
        "caption_line_spacing": ("line_spacing", "интервал подписей (по умолч. как текст)"),
        "caption_hyphenation": ("hyphenation", "переносы в подписях"),
        "caption_glue": ("glue", "предлоги и союзы не остаются в конце строки"),
        "caption_keep_with_next": ("keep_with_next",
                                   "подпись таблицы не отрывается от таблицы, рисунок от подписи"),
        "numbering": ("numbering", "нумерация рисунков/таблиц/формул: global | chapter"),
    },
    "table": {
        "table_font_size": ("font_size", "кегль ячеек (по умолч. как текст)"),
        "table_line_spacing": ("line_spacing", "интервал в ячейках"),
        "table_header_bold": ("header_bold", "шапка полужирная"),
        "table_header_align": ("header_align", "выравнивание шапки (\"\" = как столбец)"),
        "table_repeat_header": ("repeat_header", "шапка на каждой странице"),
        "table_cant_split": ("cant_split", "строка не рвётся между страницами"),
        "table_keep_together": ("keep_together", "таблица целиком на одной странице"),
        "table_continuation": ("continuation",
                               "«Продолжение таблицы N» над второй частью (вёрстка LibreOffice)"),
        "table_continuation_text": ("continuation_text", "текст над продолжением, {n} — номер"),
    },
    "list": {
        "list_layout": ("layout", "hanging (висячий отступ) | paragraph (маркер на абзацном отступе)"),
        "bullet_char": ("bullet", "маркер: \"\" = «•» шаблона, \"–\" по ГОСТ"),
        "numbered_format": ("numbered", "формат номера: \"\" = «1.», \"%1)\" = «1)»"),
        "lettered_format": ("lettered", "формат буквы r.lettered: \"{letter})\""),
        "list_letters": ("letters", "алфавит r.lettered (без ё з й о ч ъ ы ь)"),
        "list_nested_indent_cm": ("nested_indent_cm", "доп. отступ вложенного уровня, см"),
    },
    "formula": {
        "formula_space_pt": ("space_pt", "интервал до и после формулы, пт (по умолч. block_space_pt)"),
        "formula_numbering": ("numbering", "\"\" = как numbering | global | chapter"),
        "formula_where_indent": ("where_indent", "«где …» с абзацного отступа"),
    },
    "note": {
        "note_label": ("label", "одно примечание: «Примечание — …»"),
        "notes_label": ("plural_label", "несколько: «Примечания:»"),
        "example_label": ("example_label", "«Пример — …»"),
        "examples_label": ("examples_label", "«Примеры:»"),
        "note_font_size": ("font_size", "кегль примечаний"),
    },
    "keywords": {
        "keywords_min": ("min", "минимум ключевых слов"),
        "keywords_max": ("max", "максимум ключевых слов"),
        "keywords_align": ("align", "выравнивание строки ключевых слов"),
        "volume_template": ("volume_template",
                            "строка объёма реферата r.volume_line: {pages} {figures} "
                            "{tables} {sources} {sources_word} {appendices}"),
    },
    "subtitle": {
        "subtitle_italic": ("italic", "курсив"),
        "subtitle_bold": ("bold", "полужирный"),
        "subtitle_line_spacing": ("line_spacing", "интервал"),
        "subtitle_space_after": ("space_after", "после: пт или «1line»"),
        "subtitle_attach": ("attach", "интервал после заголовка уходит под подзаголовок"),
    },
    "code": {
        "code_font_name": ("font", "моноширинная гарнитура r.code"),
        "code_font_size": ("font_size", "кегль кода, пт"),
        "code_indent_cm": ("indent_cm", "отступ блока кода, см"),
    },
    "bib": {
        "bib_style": ("style", "запись источника: 7.1-2003 | 7.0.100-2018"),
        "bib_title": ("title", "заголовок списка источников"),
        "bib_align": ("align", "выравнивание записей"),
        "bib_indent_cm": ("indent_cm", "абзацный отступ записи, см"),
        "bib_min_sources": ("min_sources", "меньше источников — WARN валидатора"),
    },
    "appendix": {
        "appendix_label": ("label", "«ПРИЛОЖЕНИЕ»"),
        "appendix_letters": ("letters", "буквы приложений (без Ё З Й О Ч Ъ Ы Ь)"),
        "appendix_font_size": ("font_size", "кегль текста приложений"),
    },
    "title": {
        "title_margin_left": ("margin_left", "поля титульного листа, мм"),
        "title_margin_right": ("margin_right", ""),
        "title_margin_top": ("margin_top", ""),
        "title_margin_bottom": ("margin_bottom", ""),
        "title_size_large": ("size_large", "кегль вида работы и темы (встроенный титул)"),
        "title_right_block_indent_cm": ("right_block_indent_cm",
                                        "сдвиг блока «Выполнил/Проверил», см"),
        "title_student_layout": ("student_layout", "group_line | label_group"),
        "title_group_template": ("group_template", "строка группы, {group}"),
        "title_builder": ("builder", "callable(report): свой титул кодом (только Python)"),
        "title_blocks": ("blocks", "декларативный титул: [[title.blocks]] (см. profiles.md)"),
        "title_block_defaults": ("block_defaults", "общие ключи всех блоков: { line_spacing = 1.0 }"),
        "title_docx": ("docx", "готовый бланк титула .docx с {полями}"),
        "title_required": ("required", "обязательные поля шаблонного титула"),
        "ministry": ("ministry", "строка министерства"),
        "university_full": ("university_full", "полное название вуза"),
        "university_short": ("university_short", "краткое название вуза"),
        "faculty": ("faculty", "факультет"),
        "city": ("city", "город"),
    },
    "checks": {
        "checks": ("checks", "доп. WARN-проверки: double-space, heading-period, "
                             "paragraph-spacing, table-mention, figure-mention, "
                             "keywords-count, bib-count; [\"all\"] — все"),
    },
}

# Синонимы ключей секций сверх FIELD_DOCS (короткие имена из FIELD_DOCS — основные).
_EXTRA_ALIASES: Dict[str, Dict[str, str]] = {
    "body": {"font_name": "font_name", "first_line_indent_cm": "first_line_indent_cm",
             "auto_hyphenation": "auto_hyphenation"},
    "list": {"bullet_char": "bullet_char", "numbered_format": "numbered_format",
             "lettered_format": "lettered_format"},
    "note": {"notes_label": "notes_label"},
    "toc": {"title": "toc_title"},
    "title": {"template": "title_docx"},
}


def _profile_cls():
    import gost_report
    return gost_report.UniversityProfile


def _section_map() -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for section, fields in FIELD_DOCS.items():
        out[section] = {short: name for name, (short, _doc) in fields.items()}
    for section, extra in _EXTRA_ALIASES.items():
        out.setdefault(section, {}).update(extra)
    return out


def _field_types() -> Dict[str, Any]:
    cls = _profile_cls()
    hints = typing.get_type_hints(cls)
    return {f.name: hints.get(f.name, Any) for f in dataclasses.fields(cls)}


# ---------------------------------------------------------------------------
# Разбор файла
# ---------------------------------------------------------------------------

class StyleError(ValueError):
    """Ошибка в стилевом файле: неизвестный ключ, тип, цикл extends."""


def _read_file(path: Path) -> Dict[str, Any]:
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover - старый Python
        try:
            import tomli as tomllib  # type: ignore
        except ImportError as e:
            raise StyleError(
                f"{path}: TOML читается Python 3.11+ (tomllib) или пакетом tomli; "
                "либо сохрани стиль в .json") from e
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _suggest(key: str, options: Iterable[str]) -> str:
    near = difflib.get_close_matches(key, list(options), n=3, cutoff=0.6)
    return f" Может быть: {', '.join(near)}?" if near else ""


def flatten(data: Dict[str, Any], *, source: str = "стиль") -> Dict[str, Any]:
    """Словарь стиля (секции, синонимы) → {поле UniversityProfile: значение}.
    Ключи `extends` и `description` пропускаются (обрабатывает load_profile)."""
    types = _field_types()
    sections = _section_map()
    out: Dict[str, Any] = {}

    def put(name: str, value: Any, where: str) -> None:
        if name in out:
            raise StyleError(f"{source}: поле {name} задано дважды ({where})")
        out[name] = value

    for key, value in data.items():
        if key in ("extends", "description"):
            continue
        if key in types and not (isinstance(value, dict) and key in sections):
            if isinstance(value, dict) and key not in ("title_blocks", "title_block_defaults"):
                raise StyleError(f"{source}: {key} — поле, а не секция")
            put(key, value, key)
            continue
        if isinstance(value, dict):
            smap = sections.get(key)
            if smap is None:
                raise StyleError(
                    f"{source}: неизвестная секция [{key}]."
                    + _suggest(key, list(sections) + list(types)))
            for sub, subval in value.items():
                name = smap.get(sub)
                if name is None:
                    for cand in (f"{key}_{sub}", sub):
                        if cand in types:
                            name = cand
                            break
                if name is None:
                    raise StyleError(
                        f"{source}: в секции [{key}] нет ключа {sub!r}."
                        + _suggest(sub, smap))
                put(name, subval, f"[{key}] {sub}")
            continue
        raise StyleError(f"{source}: неизвестное поле {key!r}." + _suggest(key, types))
    return {name: _coerce(name, value, types[name], source) for name, value in out.items()}


def _coerce(name: str, value: Any, ftype: Any, source: str) -> Any:
    """Проверка типа значения по аннотации поля; списки → кортежи."""
    origin = typing.get_origin(ftype)
    args = typing.get_args(ftype)
    if origin is Union:
        if value is None and type(None) in args:
            return None
        errors = []
        for arg in args:
            if arg is type(None):
                continue
            try:
                return _coerce(name, value, arg, source)
            except StyleError as e:
                errors.append(str(e))
        raise StyleError(f"{source}: {name}={value!r}: ожидается "
                         + " или ".join(_type_name(a) for a in args if a is not type(None)))
    if origin in (tuple, Tuple):
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise StyleError(f"{source}: {name}: ожидается список, получено {value!r}")
        return tuple(dict(v) if isinstance(v, dict) else v for v in value)
    if ftype is bool:
        if not isinstance(value, bool):
            raise StyleError(f"{source}: {name}: ожидается true/false, получено {value!r}")
        return value
    if ftype is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise StyleError(f"{source}: {name}: ожидается целое, получено {value!r}")
        return value
    if ftype is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise StyleError(f"{source}: {name}: ожидается число, получено {value!r}")
        return value
    if ftype is str:
        if not isinstance(value, str):
            raise StyleError(f"{source}: {name}: ожидается строка, получено {value!r}")
        return value
    if name == "title_builder":
        raise StyleError(f"{source}: title_builder задаётся только в Python (callable)")
    return value


def _type_name(t: Any) -> str:
    return {bool: "true/false", int: "целое", float: "число", str: "строка"}.get(
        t, getattr(t, "__name__", str(t)))


# ---------------------------------------------------------------------------
# Поиск и загрузка
# ---------------------------------------------------------------------------

def _project_style_dirs(start: Optional[Path]) -> List[Path]:
    """.gost-report/styles/ вверх от start (каталог build.py / cwd)."""
    dirs: List[Path] = []
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        cand = d / ".gost-report" / "styles"
        if cand.is_dir() and cand not in dirs:
            dirs.append(cand)
        if (d / ".git").exists():
            break
    return dirs


def user_style_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "gost-report" / "styles"


def search_path(search_dirs: Sequence[Path] = ()) -> List[Path]:
    dirs: List[Path] = []
    for d in search_dirs:
        d = Path(d)
        for cand in (d, d / ".gost-report" / "styles"):
            if cand.is_dir() and cand not in dirs:
                dirs.append(cand)
    for d in _project_style_dirs(Path(search_dirs[0]) if search_dirs else None):
        if d not in dirs:
            dirs.append(d)
    for d in (user_style_dir(), BUILTIN_DIR):
        if d.is_dir() and d not in dirs:
            dirs.append(d)
    return dirs


def _looks_like_path(spec: str) -> bool:
    return (spec.lower().endswith(STYLE_SUFFIXES) or "/" in spec or "\\" in spec
            or spec.startswith("."))


def find_style(spec: str, search_dirs: Sequence[Path] = ()) -> Optional[Path]:
    """Путь к файлу стиля по имени или пути; None — не найден (тогда, может
    быть, это Python-пресет)."""
    if _looks_like_path(spec):
        p = Path(spec).expanduser()
        candidates = [p] if p.is_absolute() else (
            [Path(d) / p for d in search_dirs] + [Path.cwd() / p])
        for cand in candidates:
            if cand.is_file():
                return cand.resolve()
        raise LookupError(f"файл стиля {spec!r} не найден (искал: "
                          + ", ".join(str(c) for c in candidates) + ")")
    for d in search_path(search_dirs):
        for suffix in STYLE_SUFFIXES:
            cand = d / f"{spec}{suffix}"
            if cand.is_file():
                return cand.resolve()
    return None


def available_styles(search_dirs: Sequence[Path] = ()) -> Dict[str, str]:
    """{имя: откуда} — Python-пресеты и файлы по пути поиска (ближние побеждают)."""
    out: Dict[str, str] = {name: f"встроенный Python ({var})"
                           for name, var in _PY_PRESETS.items()}
    for d in reversed(search_path(search_dirs)):
        for f in sorted(d.iterdir()):
            if f.suffix.lower() in STYLE_SUFFIXES:
                out[f.stem] = str(f)
    return out


def load_profile(spec: Union[str, Path, Dict[str, Any], Any], *,
                 search_dirs: Sequence[Path] = (), **overrides: Any):
    """UniversityProfile из имени стиля, пути к .toml/.json, словаря или
    готового профиля; overrides — поля поверх (плоские имена)."""
    import gost_report
    cls = gost_report.UniversityProfile
    if isinstance(spec, cls):
        profile = spec
    elif isinstance(spec, dict):
        profile = _from_mapping(spec, source="dict", base_dir=None,
                                search_dirs=list(search_dirs), chain=())
    else:
        profile = _load_named(str(spec), list(search_dirs), chain=())
    if overrides:
        profile = dataclasses.replace(profile, **flatten(overrides, source="overrides"))
    gost_report._validate_profile(profile)
    return profile


def _load_named(spec: str, search_dirs: List[Path], chain: Tuple[str, ...]):
    import gost_report
    path = find_style(spec, search_dirs)
    if path is None:
        var = _PY_PRESETS.get(spec.lower())
        if var is not None:
            return getattr(gost_report, var)
        known = sorted(available_styles(search_dirs))
        raise LookupError(f"стиль {spec!r} не найден. Есть: {', '.join(known)}."
                          + _suggest(spec, known))
    key = str(path)
    if key in chain:
        raise StyleError("цикл extends: " + " → ".join(list(chain) + [key]))
    try:
        data = _read_file(path)
    except StyleError:
        raise
    except Exception as e:
        raise StyleError(f"{path}: не читается ({e})") from e
    profile = _from_mapping(data, source=str(path), base_dir=path.parent,
                            search_dirs=[path.parent, *search_dirs],
                            chain=chain + (key,))
    if "name" not in data:
        profile = dataclasses.replace(profile, name=path.stem)
    return profile


def _from_mapping(data: Dict[str, Any], *, source: str, base_dir: Optional[Path],
                  search_dirs: List[Path], chain: Tuple[str, ...]):
    import gost_report
    parents = data.get("extends")
    if parents is None:
        base = gost_report.UniversityProfile()
    else:
        if isinstance(parents, str):
            parents = [parents]
        base = None
        for parent in parents:
            loaded = _load_named(str(parent), search_dirs, chain)
            if base is None:
                base = loaded
            else:  # несколько родителей: следующий перекрывает отличия от дефолтов
                diff = {f.name: getattr(loaded, f.name)
                        for f in dataclasses.fields(loaded)
                        if getattr(loaded, f.name) != f.default
                        and f.name not in ("name", "base_dir")}
                base = dataclasses.replace(base, **diff)
    values = flatten(data, source=source)
    if base_dir is not None and "base_dir" not in values:
        values["base_dir"] = str(base_dir)
    return dataclasses.replace(base, **values)


# ---------------------------------------------------------------------------
# Вывод: TOML без зависимостей
# ---------------------------------------------------------------------------

def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(x)}" for k, x in v.items()) + " }"
    return json.dumps(str(v), ensure_ascii=False)


def dump_toml(profile, *, only_changed: bool = True) -> str:
    """Профиль → TOML по секциям FIELD_DOCS (по умолчанию только отличия от ГОСТ)."""
    cls = type(profile)
    defaults = cls()
    lines: List[str] = []
    blocks = None
    # Поля «верхнего уровня» (секции из одного одноимённого поля) — до таблиц:
    # в TOML всё после заголовка [секции] принадлежит ей.
    for section in ("meta", "checks"):
        for name, (short, _doc) in FIELD_DOCS[section].items():
            value = getattr(profile, name)
            if name != "base_dir" and value is not None and not (
                    only_changed and value == getattr(defaults, name)):
                lines.append(f"{name} = {_toml_value(value)}")
    for section, fields in FIELD_DOCS.items():
        if section in ("meta", "checks"):
            continue
        rows = []
        for name, (short, _doc) in fields.items():
            if name in ("base_dir", "title_builder"):
                continue
            value = getattr(profile, name)
            if only_changed and value == getattr(defaults, name):
                continue
            if value is None:
                continue
            if name == "title_blocks":
                blocks = value
                continue
            rows.append(f"{short} = {_toml_value(value)}")
        if rows:
            lines.append(f"\n[{section}]")
            lines.extend(rows)
    if blocks:
        for block in blocks:
            lines.append("\n[[title.blocks]]")
            for k, v in block.items():
                lines.append(f"{k} = {_toml_value(v)}")
    return "\n".join(lines).strip() + "\n"


def check_docs() -> List[str]:
    """Поля UniversityProfile без описания в FIELD_DOCS (для теста)."""
    documented = {n for fields in FIELD_DOCS.values() for n in fields}
    return [f.name for f in dataclasses.fields(_profile_cls()) if f.name not in documented]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_fields(section: Optional[str]) -> int:
    cls = _profile_cls()
    defaults = cls()
    types = _field_types()
    secs = [section] if section else list(FIELD_DOCS)
    for sec in secs:
        if sec not in FIELD_DOCS:
            print(f"нет секции {sec!r}; есть: {', '.join(FIELD_DOCS)}", file=sys.stderr)
            return 2
        print(f"[{sec}]")
        for name, (short, doc) in FIELD_DOCS[sec].items():
            default = getattr(defaults, name)
            if name == "title_builder":
                shown = "None"
            elif isinstance(default, tuple) and len(default) > 4:
                shown = f"[{len(default)} знач.]"
            else:
                shown = _toml_value(default) if default is not None else "—"
            tname = getattr(types[name], "__name__", None) or str(types[name]).replace("typing.", "")
            print(f"  {short:<22} {name:<28} = {shown:<14} {doc}  ({tname})")
        print()
    return 0


def _reexec_in_venv(argv: List[str]) -> None:
    """Описание полей берётся из gost_report (нужен python-docx): без него
    перезапуск под питоном venv скилла, как у validate.py."""
    try:
        import docx  # noqa: F401
        return
    except ImportError:
        pass
    try:
        import ensure_env
        venv_py = ensure_env.venv_python()
    except Exception:
        return
    if not venv_py.exists() or Path(sys.executable).resolve() == venv_py.resolve():
        return
    if os.name == "nt":
        import subprocess
        sys.exit(subprocess.run([str(venv_py), __file__, *argv]).returncode)
    os.execv(str(venv_py), [str(venv_py), __file__, *argv])


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sys.path.insert(0, str(SCRIPTS_DIR))
    if argv and argv[0] not in ("-h", "--help"):
        _reexec_in_venv(argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "list":
            for name, where in available_styles([Path.cwd()]).items():
                print(f"{name:<16} {where}")
            return 0
        if cmd == "fields":
            return _cmd_fields(rest[0] if rest else None)
        if cmd in ("show", "check"):
            if not rest:
                print(f"styles.py {cmd} <имя|путь>", file=sys.stderr)
                return 2
            profile = load_profile(rest[0], search_dirs=[Path.cwd()])
            if cmd == "check":
                print(f"ok: {rest[0]} → профиль «{profile.name or 'без имени'}»")
                return 0
            print(dump_toml(profile, only_changed="--all" not in rest), end="")
            return 0
    except (LookupError, ValueError) as e:
        print(f"styles.py: {e}", file=sys.stderr)
        return 1
    print(f"styles.py: неизвестная команда {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
