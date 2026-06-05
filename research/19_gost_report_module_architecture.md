# Архитектура расширяемых модулей gost-report: «LaTeX-of-docx»

**Статус:** Proposed
**Дата:** 2026-06-05
**Контекст:** `skills/gost-report/scripts/gost_report.py` (2086 строк) — монолит. Цель: маленькое стабильное ядро + много opt-in модулей (viz, diagrams, bib, code, data, …), которые «штампуются» по шаблону, выглядят одинаково при использовании, и все разделяют ОДИН счётчик рисунков/таблиц, embedding и контракт byte-стабильности. Дистрибуция — папка-скилл, копируемая verbatim (без pip), при этом надо открыть дорогу к pip-публикации `gost-report-<module>` третьими лицами.

Код/идентификаторы — на английском. Проза — RU/EN по месту.

---

## 0. Краткий обзор решения (TL;DR)

1. **Seam, не rewrite.** `gost_report.py` распиливается на пакет `gost_report/` минимальными движениями: ядро = `Report` + `UniversityProfile`/`TitleConfig` + counters + figure/table embedding + determinism + `_paths`/env. OMML-пайплайн (≈900 строк, строки 693–1262) выезжает в **первый встроенный модуль** `gost_report_math` — он уже самодостаточный подграф и служит образцом «модуля».
2. **Контракт модуля — Protocol `ReportModule`** + namespaced attach (`r.plot.line(...)`, `r.diagram(...)`). Модуль НЕ переизобретает подпись/нумерацию: он производит PNG и зовёт **shared core service** `r.core.embed_figure(png, caption)`.
3. **Единый embeddable-протокол `Figure`** (структурная типизация): chart, diagram, raw-PNG взаимозаменяемы на call-site. `r.figure(x, caption)` принимает путь ИЛИ любой `Figure`.
4. **Два мира discovery:** built-in registry для vendored-модулей (внутри папки скилла) + опциональные `importlib.metadata` entry points для pip-установленных `gost-report-*`. Один и тот же `ReportModule`-контракт, два загрузчика.
5. **Тиры зависимостей:** lightweight default остаётся `python-docx`+`latex2mathml`. `requirements.txt` → `requirements.d/<extra>.txt` + `pyproject.toml` extras (`[viz]`, `[diagrams]`, …). `ensure_env.py` учится включать тиры по env-флагу. Lazy-import везде.
6. **«Stamp a module»:** `scripts/new_module.py` генерит папку модуля по шаблону за один вызов; авторинг — ≤8 шагов, скучный и одинаковый.

---

## 1. Контекст и ограничения, которые архитектура ОБЯЗАНА соблюсти

| Сила | Следствие для архитектуры |
|---|---|
| Все визуалы (PNG, matplotlib, mermaid) — это «иллюстрация» по ГОСТ 7.32, делят ОДИН непрерывный счётчик «Рисунок N — …» | Счётчик рисунков/таблиц — **shared core service**, не приватное поле `Report`. Модуль увеличивает тот же `_figure_counter`, что и `r.figure()`. |
| python-docx встраивает только растр (PNG/JPEG) | Любой визуальный модуль в конце концов производит PNG и отдаёт его core-embed. Векторные форматы (SVG) рендерятся в PNG@300dpi внутри модуля. |
| Byte-deterministic output — существующая цель (`_strip_python_docx_fingerprints`, `_strip_thumbnail_part`) | Каждый модуль обязан соблюдать determinism-контракт: фиксированные метаданные PNG, без таймстемпов/random-id где возможно. Где невозможно (mermaid SVG ids) — задокументированная деградация. |
| Дистрибуция — папка скилла, копируемая verbatim; **нет pip** на стороне пользователя | Vendored-модули живут внутри папки и регистрируются built-in реестром. Pip-extras — параллельная, опциональная вселенная для третьих лиц. |
| Codex target (`~/.codex/skills/`) ставит только skills, без agents/commands | Discovery и тиры не должны зависеть от agent-инфраструктуры. Всё внутри `scripts/`. |
| `Report._resolve`, env-резолв, `paths()`, валидатор уже завязаны на `gost_report` | Ядро экспортирует их как стабильный публичный slim-API. Модули зовут только публичные core-сервисы, не приватные `_методы`. |

---

## 2. Декомпозиция монолита (минимальный seam)

### 2.1. Текущая структура `gost_report.py` (по строкам)

| Блок | Строки | Куда едет |
|---|---|---|
| Константы ГОСТ (`FONT_NAME`, размеры, поля) | 60–87 | **core** `gost_report/_const.py` |
| `UniversityProfile`, `GOST/ITMO_PROFILE` | 93–167 | **core** `gost_report/profile.py` |
| `StyledRun`, `GostValidationError` | 181–208 | **core** `gost_report/_types.py` |
| `TitleConfig` + env-резолв (`_config_file_path`…`_resolve_student_label`) | 216–405 | **core** `gost_report/title.py` |
| run/font/section/determinism helpers (`_force_run_fonts`…`_configure_heading_style`) | 408–614 | **core** `gost_report/_docx_util.py` |
| `_sanitize_prose` | 616–692 | **core** `gost_report/prose.py` (публичный — модули должны санировать captions) |
| **Весь OMML/MathML пайплайн** (`_ml_local`…`_latex_to_omath`) | 693–1262 | **МОДУЛЬ** `gost_report_math/` (первый extracted module) |
| `class Report` | 1269–2086 | **core** `gost_report/report.py` — но `formula()` становится тонкой обёрткой над модулем math |

### 2.2. Целевая раскладка пакета

```
scripts/
  gost_report/                  ← core package (был один .py)
    __init__.py                 ← re-export: Report, TitleConfig, UniversityProfile, paths, profiles
    _const.py                   ← ГОСТ-инварианты
    _types.py                   ← StyledRun, GostValidationError
    _docx_util.py               ← font/section/determinism helpers (был _force_run_fonts…)
    prose.py                    ← sanitize_prose (публичный)
    profile.py                  ← UniversityProfile + пресеты
    title.py                    ← TitleConfig + env resolve
    report.py                   ← class Report + CoreServices
    core_api.py                 ← Protocol-ы: ReportModule, Figure, CoreServices (контракты)
    registry.py                 ← discovery: built-in + entry_points
    _paths.py                   ← (как есть)
    validate.py                 ← (как есть, импорт чинится)
  gost_report_math/             ← module #0 (extracted OMML)
    __init__.py
    module.py                   ← MathModule(ReportModule)
    _omml.py                    ← весь _walk_mathml/_handle_* (verbatim из монолита)
  gost_report_viz/              ← module #1 (matplotlib)
  gost_report_diagrams/         ← module #2 (mermaid)
  ensure_env.py                 ← учится про тиры
  requirements.txt              ← остаётся lightweight default
  requirements.d/
    viz.txt
    diagrams.txt
```

**Backward-compat:** `gost_report/__init__.py` re-export всего, что сейчас на верхнем уровне (`Report, TitleConfig, UniversityProfile, GOST_PROFILE, ITMO_PROFILE, paths, GostValidationError`). Существующие `from gost_report import Report, TitleConfig` продолжают работать без изменений — `.pth` указывает на `scripts/`, `gost_report` теперь пакет вместо модуля, импорт-путь идентичен. `r.formula(...)` сохраняет сигнатуру и поведение. **Churn для пользователя = ноль.**

**Объём работы по факту:** перенос блоков в файлы (mechanical), + ~120 строк нового кода (`core_api.py`, `registry.py`, тонкая обёртка `formula`). Не переписываем 2086 строк — режем по уже существующим швам (OMML давно изолирован, helpers уже свободные функции).

### 2.3. Что становится `CoreServices`

`Report` уже содержит ровно те примитивы, которые нужны модулям. Выносим их в фасад `CoreServices`, отдаваемый модулю при attach. Существующие приватные методы переименовываются в публичные (тонкая работа):

| Сейчас в `Report` | Становится core service | Зачем модулю |
|---|---|---|
| `_figure_counter` + блок подписи в `figure()` (1785, 1812) | `embed_figure(image_path, caption, *, width_cm=None) -> int` | единая нумерация+подпись рисунков |
| `_table_counter` + блок в `table()` (1893–1901) | `embed_table(rows, caption, has_header=True) -> int` | единая нумерация таблиц |
| `_formula_counter` | `next_formula_number() -> int` | math-модуль |
| `_printable_cm()` (1404) | `printable_cm() -> float` | clamp ширины визуала |
| `_make_paragraph/_add_paragraph` (1412, 1444) | `paragraph(...)`, `styled_run_paragraph(...)` | модулям, которые пишут прозу (bib, code) |
| `_sanitize_prose` | `prose.sanitize(text)` | captions/прозовый контент |
| `_resolve_figure_path` (1745) | `resolve_figure_path(path) -> Path` | модулям с файлами |
| `self._paths` | `paths -> ProjectPaths` | tmp-файлы рядом с `docs/` |
| `self._profile` | `profile -> UniversityProfile` | viz берёт поля для размера холста |
| `_doc` | `doc -> Document` (escape hatch) | редко; не для типового модуля |
| theme/font registry (новое) | `assets -> AssetRegistry` | бандл-шрифты (Liberation Serif) и темы |

---

## 3. Контракт модуля (Protocol + sketch)

Используем `typing.Protocol` (структурная типизация), не ABC: модули из pip не обязаны импортировать наш ABC, чтобы «считаться» модулем — достаточно совпадения формы. Это снижает coupling для third-party.

### 3.1. `core_api.py` — контракты

```python
# gost_report/core_api.py
from __future__ import annotations
from pathlib import Path
from typing import Protocol, runtime_checkable, Optional, Sequence, Mapping


@runtime_checkable
class Figure(Protocol):
    """Что угодно встраиваемое как «иллюстрация» по ГОСТ.

    Контракт: render() детерминированно производит PNG на диск и возвращает путь.
    Caption и нумерацию делает core (embed_figure), НЕ объект Figure.
    """
    def render(self, *, dpi: int = 300, max_width_cm: float) -> Path: ...
    # Подсказка ширины — натуральная ширина в см или None (core клампит).
    @property
    def natural_width_cm(self) -> Optional[float]: ...


@runtime_checkable
class CoreServices(Protocol):
    """Shared-сервисы ядра, которые модуль ТОЛЬКО потребляет.

    Модуль никогда не трогает w:figure-нумерацию, подписи, determinism сам —
    он зовёт сюда. Это и есть «никогда не переизобретай caption/numbering».
    """
    # --- единый embed + единый счётчик ---
    def embed_figure(self, image_path: Path | str, caption: str, *,
                     width_cm: Optional[float] = None) -> int: ...
    def embed_table(self, rows: Sequence[Sequence[str]], caption: str = "",
                    *, has_header: bool = True) -> int: ...
    def next_formula_number(self) -> int: ...

    # --- геометрия/профиль ---
    def printable_cm(self) -> float: ...
    @property
    def profile(self): ...          # UniversityProfile
    @property
    def paths(self): ...            # ProjectPaths (root/docs/figures/...)

    # --- прозовый контент (для bib/code/glossary) ---
    def paragraph(self, *args, **kwargs): ...
    def sanitize(self, text: str) -> str: ...

    # --- бандл-ассеты (шрифты, темы) ---
    @property
    def assets(self) -> "AssetRegistry": ...

    # --- escape hatch (использовать редко) ---
    @property
    def doc(self): ...              # docx.Document


@runtime_checkable
class ReportModule(Protocol):
    """Контракт расширения. Жизненный цикл: construct → check_available →
    attach(core) → (использование через r.<namespace>) → teardown.
    """
    #: имя атрибута на Report: r.<namespace> (например "plot", "diagram", "bib")
    namespace: str
    #: человекочитаемое имя для ошибок
    title: str
    #: extra-ключ для тиров зависимостей: "viz", "diagrams", ...
    requires_extra: Optional[str]

    def check_available(self) -> None:
        """Бросает ActionableImportError с инструкцией, если deps нет.
        Вызывается лениво при ПЕРВОМ обращении к r.<namespace>, не при attach —
        чтобы default-документ без графиков не требовал matplotlib."""
        ...

    def attach(self, core: CoreServices) -> "ModuleAPI":
        """Возвращает объект, который станет r.<namespace>.
        ModuleAPI хранит ссылку на core и реализует line/scatter/... ."""
        ...

    def teardown(self) -> None:
        """Чистка tmp-файлов, закрытие matplotlib figures и т.п. Зовётся из
        Report.save() и Report.__del__. Идемпотентен."""
        ...
```

### 3.2. Actionable ImportError (graceful degradation)

```python
# gost_report/core_api.py (continued)
class ActionableImportError(ImportError):
    """ImportError с конкретной командой-починкой, завязанной на тиры."""

    @classmethod
    def for_extra(cls, module_title: str, extra: str, missing: str) -> "ActionableImportError":
        return cls(
            f"{module_title} требует пакет '{missing}' (тир '{extra}'). "
            f"Если запускаешь через scripts/ensure_env.py — включи тир:\n"
            f"    GOST_REPORT_EXTRAS={extra} python3 scripts/ensure_env.py build.py\n"
            f"или один раз: GOST_REPORT_EXTRAS={extra} python3 scripts/ensure_env.py\n"
            f"Pip-режим: pip install 'gost-report[{extra}]'"
        )
```

### 3.3. Namespacing: `r.plot.line(...)` vs flat `r.line(...)` — **решение**

**Namespaced (`r.plot.line`, `r.diagram(...)`, `r.bib.cite(...)`).** Обоснование:

- **Нет коллизий имён** между модулями. `bar` у viz и `bar` у будущего gantt-модуля не конфликтуют. Flat-API в экосистеме «куча библиотек как в LaTeX» гарантированно даст коллизии.
- **Discoverability** в IDE: `r.plot.<TAB>` показывает ровно методы viz.
- **Чистый attach**: модуль вешает ОДИН атрибут `r.<namespace>`, а не патчит N методов на `Report` (меньше магии, меньше риска перетереть core-метод).
- **Единичные модули** (diagram, который logically «одна функция») экспонируются как **callable namespace**: `r.diagram(src, caption)` работает, потому что объект-namespace реализует `__call__`, а `r.diagram.theme(...)` доступен для конфигурации. Лучшее из обоих миров.

Исключение — `r.figure(...)` и `r.formula(...)` остаются **flat на core** (BC + они каноничны). `formula` внутри делегирует math-модулю.

---

## 4. Embeddable-протокол `Figure` (унифицирующая эргономика)

Цель: chart, diagram и raw-PNG взаимозаменяемы на call-site. Достигается тем, что `r.figure()` (и `embed_figure`) принимает `Path | str | Figure`.

### 4.1. Расширение `embed_figure` в ядре

```python
# gost_report/report.py  (внутри Report, новый core-метод)
def embed_figure(self, image, caption, *, width_cm=None) -> int:
    """image: путь к PNG/JPEG ИЛИ любой Figure (chart, diagram).
    Единственная точка нумерации+подписи рисунков для ВСЕХ модулей."""
    if isinstance(image, Figure):           # structural check (runtime_checkable)
        png = image.render(dpi=300, max_width_cm=self.printable_cm())
        nat = image.natural_width_cm
    else:
        png = self._resolve_figure_path(image)
        nat = None
    self._figure_counter += 1
    # ... существующий код clamp+add_picture+подпись (строки 1787–1817) ...
    # подпись формируется ТОЛЬКО здесь: f"Рисунок {n} — {sanitize(caption)}"
    return self._figure_counter
```

`r.figure(...)` (публичный, BC) = тонкий враппер над `embed_figure`. Модули зовут `core.embed_figure(...)`.

### 4.2. Три call-site, одинаковых на вид

```python
# raw PNG (как сегодня)
r.figure("schema.png", "Архитектура сети")

# chart из viz — возвращает Figure, идёт в тот же r.figure
chart = r.plot.line(xs, ys, xlabel="t, с", ylabel="U, В")
r.figure(chart, "Переходная характеристика")

# или fluent: модуль сам зовёт embed, если передан caption
r.plot.line(xs, ys, caption="Переходная характеристика")   # эквивалент

# diagram из mermaid — тоже Figure
d = r.diagram("graph LR; A-->B; B-->C", caption=None)
r.figure(d, "Схема пайплайна")
```

Все три инкрементят ОДИН счётчик: «Рисунок 1 …», «Рисунок 2 …», «Рисунок 3 …» подряд, независимо от того, raw это или сгенерированный визуал. Это и есть требование ГОСТ, выраженное в типах.

### 4.3. Конкретный `LineChart(Figure)` из viz

```python
# gost_report_viz/charts.py
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence
import hashlib

@dataclass
class LineChart:
    xs: Sequence[float]
    series: Sequence[Sequence[float]]
    labels: Sequence[str] = field(default_factory=list)
    xlabel: str = ""
    ylabel: str = ""
    _cache: Optional[Path] = field(default=None, init=False, repr=False)

    natural_width_cm = 16.0   # ГОСТ-печатная ширина по умолчанию

    def render(self, *, dpi: int = 300, max_width_cm: float) -> Path:
        if self._cache and self._cache.exists():
            return self._cache
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from .style import apply_gost_style, OKABE_ITO, second_channel
        apply_gost_style()                       # rcParams preset, Liberation Serif, stix
        w_in = min(self.natural_width_cm, max_width_cm) / 2.54
        fig, ax = plt.subplots(figsize=(w_in, w_in * 0.62), dpi=dpi)
        for i, ys in enumerate(self.series):
            ax.plot(self.xs, ys, color=OKABE_ITO[i % 8], **second_channel(i),
                    label=self.labels[i] if i < len(self.labels) else None)
        ax.set_xlabel(self.xlabel); ax.set_ylabel(self.ylabel)
        if self.labels: ax.legend()
        # детерминированное имя по содержимому → стабильный путь, идемпотентный rerun
        key = hashlib.sha256(repr((self.xs, self.series, self.labels)).encode()).hexdigest()[:16]
        out = Path(_tmp_dir()) / f"viz_{key}.png"
        fig.savefig(out, dpi=dpi, bbox_inches="tight",
                    metadata={"Software": None, "CreationTime": None})  # determinism
        plt.close(fig)
        self._cache = out
        return out
```

`metadata={'Software': None}` + детерминированное имя файла по хэшу контента = byte-стабильный PNG между запусками (при фиксированных версиях matplotlib/freetype, которые пинятся в `requirements.d/viz.txt`).

---

## 5. Модуль math как образец (extracted module #0)

```python
# gost_report_math/module.py
from gost_report.core_api import ReportModule, CoreServices, ActionableImportError

class _MathAPI:
    def __init__(self, core: CoreServices):
        self._core = core
    def formula(self, latex: str, *, where=None) -> int:
        from ._omml import latex_to_omath           # lazy
        n = self._core.next_formula_number()
        omath = latex_to_omath(latex)
        # paragraph + tabs + omath append — переезжает из Report.formula() 1848–1885
        ...
        return n

class MathModule:
    namespace = "math"
    title = "Формулы (LaTeX → OMML)"
    requires_extra = None                  # latex2mathml в lightweight default

    def check_available(self):
        try:
            import latex2mathml  # noqa
        except ImportError as e:
            raise ActionableImportError.for_extra(self.title, "core", "latex2mathml") from e
    def attach(self, core): return _MathAPI(core)
    def teardown(self): pass
```

`Report.formula(...)` остаётся как BC-shim: `return self.math.formula(latex, where=where)`. Модуль math регистрируется как built-in и **auto-attach** (потому что `latex2mathml` в default-тире). Так доказываем, что монолитная подсистема превращается в модуль без потери API.

---

## 6. Discovery и дистрибуция (два мира)

### 6.1. `registry.py` — единый контракт, два загрузчика

```python
# gost_report/registry.py
from __future__ import annotations
import importlib, pkgutil
from typing import Dict
from .core_api import ReportModule

# (A) Built-in vendored: модули внутри папки скилла. Перечень — явный, не
#     auto-scan (детерминизм + контроль над тем, что auto-attach).
_BUILTIN = {
    "math":     "gost_report_math.module:MathModule",
    "plot":     "gost_report_viz.module:VizModule",
    "diagram":  "gost_report_diagrams.module:DiagramModule",
}
# math auto-attach (deps в default); остальные — lazy on first use.
_AUTO_ATTACH = {"math"}

def _load(spec: str) -> ReportModule:
    mod_path, _, cls = spec.partition(":")
    return getattr(importlib.import_module(mod_path), cls)()

def discover() -> Dict[str, ReportModule]:
    found: Dict[str, ReportModule] = {}
    # (A) built-in
    for ns, spec in _BUILTIN.items():
        try:
            found[ns] = _load(spec)
        except ImportError:
            pass        # модуль не vendored в этом срезе — ок
    # (B) pip entry points (опционально, только если установлен pip-пакет)
    try:
        from importlib.metadata import entry_points
        eps = entry_points(group="gost_report.modules")
        for ep in eps:
            try:
                m = ep.load()()
                found.setdefault(m.namespace, m)   # built-in приоритетнее
            except Exception:
                pass
    except Exception:
        pass
    return found
```

**Два мира примирены:** vendored-модуль = строка в `_BUILTIN` (работает в папке-скилле, без pip, и на Claude, и на Codex target). Pip-модуль третьего лица объявляет в своём `pyproject.toml`:

```toml
[project.entry-points."gost_report.modules"]
chem = "gost_report_chem.module:ChemModule"
```

…и `discover()` подхватит его через `importlib.metadata` — **только если** он реально установлен в активный Python. Codex/skills-only ничего про entry points не знает и не должен — для него работает ветка (A). Никакой зависимости от agents/commands.

### 6.2. Lazy attach в `Report`

```python
# gost_report/report.py
class Report:
    def __init__(self, ...):
        ...
        self._modules = discover()
        self._attached = {}
        self._core = self          # Report сам реализует CoreServices Protocol
        for ns in _AUTO_ATTACH:
            if ns in self._modules:
                self._attach(ns)

    def _attach(self, ns: str):
        mod = self._modules[ns]
        mod.check_available()                    # actionable error здесь
        self._attached[ns] = mod.attach(self._core)
        return self._attached[ns]

    def __getattr__(self, name):
        # вызывается ТОЛЬКО если обычный атрибут не найден → ленивый attach
        mods = self.__dict__.get("_modules", {})
        if name in mods:
            if name not in self._attached:
                return self._attach(name)        # lazy: matplotlib грузится тут
            return self._attached[name]
        raise AttributeError(name)
```

`r.plot` триггерит `check_available()` → если matplotlib нет, пользователь получает ActionableImportError с командой `GOST_REPORT_EXTRAS=viz …`. Default-документ без `r.plot` никогда не импортирует matplotlib — lightweight остаётся чистым.

### 6.3. Installer / Codex

- `install.sh` / `install.ps1` копируют папку скилла verbatim — `gost_report_viz/` и т.д. едут как обычные подпапки `scripts/`. Ноль изменений в логике installer (он копирует skill-folder целиком).
- `--target codex` идентичен: те же папки в `~/.codex/skills/gost-report/scripts/`. Entry-points ветка просто не сработает (нет pip-пакетов) — graceful.
- Venv общий (ADR-008, keyed by skill name) — тиры доустанавливаются в тот же venv по требованию (см. §7).

---

## 7. Тиры зависимостей и packaging

### 7.1. Раскладка extras

`requirements.txt` (default, без изменений):
```
python-docx>=1.1.0
latex2mathml>=3.77.0
```

`requirements.d/viz.txt` (пинится для determinism):
```
numpy==2.1.*
matplotlib==3.9.*        # freetype/agg backend — стабильный растр
fonttools==4.*           # для бандла Liberation Serif (не тащим scipy/pandas)
```

`requirements.d/diagrams.txt`:
```
mermaid-py>=0.x          # «merm» pure-python default backend
cairosvg>=2.7            # SVG→PNG @300dpi (pip-only, без Node)
```

`pyproject.toml` (для pip-мира; в папке-скилле не используется, но обязателен для third-party и для будущей pip-публикации самого core):
```toml
[project]
name = "gost-report"
dynamic = ["version"]
dependencies = ["python-docx>=1.1.0", "latex2mathml>=3.77.0"]

[project.optional-dependencies]
viz      = ["numpy>=2.1,<2.2", "matplotlib>=3.9,<3.10", "fonttools>=4"]
diagrams = ["mermaid-py", "cairosvg>=2.7"]
bib      = []                       # ГОСТ Р 7.0.5 — pure-python, без deps
code     = ["pygments>=2.17"]
data     = ["pandas>=2.2"]          # опционально; CSV без pandas тоже умеем
all      = ["gost-report[viz,diagrams,code,data]"]
```

### 7.2. `ensure_env.py` учится про тиры (минимальная правка)

Сейчас `ensure_env.py` хэширует один `requirements.txt`. Расширение:

```python
# ensure_env.py — псевдодифф
def _active_extras() -> list[str]:
    raw = os.environ.get("GOST_REPORT_EXTRAS", "")   # "viz,diagrams"
    return [e.strip() for e in raw.split(",") if e.strip()]

def _req_files() -> list[Path]:
    files = [REQ_FILE]
    for e in _active_extras():
        f = SCRIPTS_DIR / "requirements.d" / f"{e}.txt"
        if f.exists():
            files.append(f)
    return files

def req_hash() -> str:               # хэш по ВСЕМ активным req-файлам
    h = hashlib.sha256()
    for f in sorted(_req_files()):
        h.update(f.read_bytes())
    return h.hexdigest()
```

Хэш включает активные тиры → переключение `GOST_REPORT_EXTRAS=viz` триггерит доустановку в тот же venv, тёплый запуск без тиров остаётся 30 мс no-op. Лёгкий дефолт не тащит 66 МБ matplotlib, пока кто-то реально не позвал `r.plot`.

### 7.3. Vendoring vs pip-extras — честное примирение

| | Папка-скилл (сегодня) | Pip-пакет (third-party) |
|---|---|---|
| **Код модуля** | vendored в `scripts/gost_report_viz/` | `pip install gost-report-chem` |
| **Регистрация** | строка в `_BUILTIN` | `entry-points` в их `pyproject` |
| **Тяжёлые deps** | `requirements.d/<extra>.txt` + `GOST_REPORT_EXTRAS` | extras `gost-report[viz]` |
| **Бандл-ассеты (шрифты)** | файлы в папке модуля (verbatim copy) | package_data в wheel |

**Решение:** core + viz + diagrams + math **vendored** (контролируем determinism и качество — это «стандартная библиотека»). Экзотику (chem, circuits, plantuml) оставляем pip-экосистеме — так и рождается «куча библиотек как в LaTeX», без раздувания базового скилла. Бандл-шрифты (Liberation Serif, SIL OFL) кладём в `gost_report_viz/assets/fonts/` и копируем verbatim — это безопасно по лицензии (OFL), не зависит от системных шрифтов и держит viz детерминированным.

---

## 8. «Stamp a module» — DX

### 8.1. Кукбук авторинга (≤8 шагов)

1. `python3 scripts/new_module.py <namespace>` — генерит `gost_report_<namespace>/` из шаблона.
2. Вписать `requires_extra` и (если нужно) создать `requirements.d/<extra>.txt` с пинами.
3. Реализовать `check_available()` — один try/except + `ActionableImportError.for_extra(...)`.
4. Если модуль производит визуал — реализовать класс `Figure` с `render()→PNG` (deterministic savefig).
5. Реализовать `_<Ns>API` — публичные методы (`line/scatter/...`), которые зовут `self._core.embed_figure(...)`. Caption/нумерацию НЕ писать руками.
6. Зарегистрировать: добавить строку в `_BUILTIN` (vendored) ИЛИ entry-point в `pyproject` (pip).
7. `GOST_REPORT_EXTRAS=<extra> python3 scripts/ensure_env.py` — поставить тир в venv.
8. Прогнать пример из `gost_report_<namespace>/example.py`; визуально проверить «Рисунок N — …».

### 8.2. Что эмитит `scripts/new_module.py`

```python
#!/usr/bin/env python3
"""Stamp a gost-report module skeleton. Usage: new_module.py <namespace> [--visual]"""
import sys, textwrap
from pathlib import Path

NS = sys.argv[1]
VISUAL = "--visual" in sys.argv
ROOT = Path(__file__).resolve().parent / f"gost_report_{NS}"
ROOT.mkdir(exist_ok=False)

(ROOT / "__init__.py").write_text("")

MODULE = textwrap.dedent(f'''\
    from gost_report.core_api import ReportModule, CoreServices, ActionableImportError

    class _{NS.capitalize()}API:
        def __init__(self, core: CoreServices):
            self._core = core

        def example(self, caption: str) -> int:
            # TODO: построить контент; для визуала верни Figure в embed_figure.
            png = ...  # produce PNG (see render() pattern)
            return self._core.embed_figure(png, caption)

    class {NS.capitalize()}Module:
        namespace = "{NS}"
        title = "TODO человекочитаемое имя"
        requires_extra = "{NS}"   # None если deps в default

        def check_available(self):
            try:
                import {NS}_dep  # TODO заменить на реальный пакет
            except ImportError as e:
                raise ActionableImportError.for_extra(
                    self.title, self.requires_extra, "{NS}_dep") from e

        def attach(self, core): return _{NS.capitalize()}API(core)
        def teardown(self): pass
''')
(ROOT / "module.py").write_text(MODULE)

if VISUAL:
    (ROOT / "figure.py").write_text(textwrap.dedent('''\
        from dataclasses import dataclass, field
        from pathlib import Path
        from typing import Optional
        import hashlib

        @dataclass
        class _Fig:
            natural_width_cm: Optional[float] = 16.0
            _cache: Optional[Path] = field(default=None, init=False)
            def render(self, *, dpi=300, max_width_cm):
                # TODO produce deterministic PNG; savefig(metadata={"Software": None})
                ...
    '''))

print(f"Stamped gost_report_{NS}/  ->  add registry._BUILTIN entry and requirements.d/{NS}.txt")
```

Авторинг становится скучным и одинаковым — ровно цель «штамповать модули как LaTeX-пакеты».

---

## 9. Roadmap модулей (ранжировано по value/effort)

Effort: S ≤1 день, M ≤3 дня, L неделя+. Det.risk = риск нарушить byte-стабильность.

| # | Модуль | Назначение (1 строка) | Deps / тир | Det.risk | Effort | Core-сервисы |
|---|---|---|---|---|---|---|
| 0 | **math** (extract) | LaTeX→OMML формулы (уже есть, выезжает в модуль) | latex2mathml / default | низкий | S | next_formula_number, paragraph |
| 1 | **viz** | matplotlib charts → PNG@300, GOST style | numpy+mpl / `[viz]` | средний (пинами держим) | M | embed_figure, printable_cm, assets(fonts) |
| 2 | **diagrams** | mermaid → SVG→PNG, GOST theme | mermaid-py+cairosvg / `[diagrams]` | **высокий** (random SVG ids) — задокументировать | M | embed_figure, printable_cm, assets(theme) |
| 3 | **code** | листинги pygments→styled docx runs, моноширинно | pygments / `[code]` | низкий | S | paragraph, styled_run |
| 4 | **bib** | ГОСТ Р 7.0.5 список литературы + `\cite` нумерация, BibTeX import | none (pure-py) / default | низкий | M | paragraph, sanitize, cross-ref counter |
| 5 | **data** | CSV/DataFrame → `embed_table` с авто-нумерацией | pandas optional / `[data]` | низкий (CSV deterministic) | S | embed_table |
| 6 | **xref** | авто-кросс-ссылки «см. рисунок N / таблицу N / формулу (N)» по меткам | none / default | низкий | M | все counters (read), paragraph |
| 7 | **units** | siunitx-аналог: `r.units.q(9.8, "м/с^2")` → корректная типографика | none / default | низкий | S | paragraph (или inline в text) |
| 8 | **glossary** | список сокращений/терминов, авто-сбор по тексту | none / default | низкий | M | paragraph, sanitize |
| 9 | **graphviz** | DOT-графы → PNG (альтернатива mermaid, точнее) | graphviz (system) / `[graphviz]` | средний | M | embed_figure |
| 10 | **appendix** | приложения А/Б/В с собственной нумерацией рисунков «Рисунок А.1» | none / default | низкий | L | counter-namespacing (нужна доработка core) |
| 11 | **plantuml** | UML-диаграммы (Java/server) | сеть/Java / `[plantuml]` | высокий | L | embed_figure |
| 12 | **chem/circuits** | mhchem-аналог, схемы | специфичные / pip third-party | высокий | L | embed_figure / math |

**Порядок реализации:** 0→1→2 (заданы исследованием), затем **3 (code)** и **5 (data)** как самые дешёвые pure-функции, затем **4 (bib)** и **6 (xref)** как высокоценные для ВКР/курсовых. Appendix (#10) триггерит доработку core (namespaced counters «А.1») — отложить до момента, когда seam доказан.

---

## 10. Риски и трейд-оффы

| Риск | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `__getattr__`-магия ломает интроспекцию/сериализацию/отладку | Средняя | Средний | `__getattr__` срабатывает только для известных namespaces из `discover()`; всё прочее → честный `AttributeError`. Документировать namespaces. |
| Determinism у diagrams недостижим (random SVG ids, font metrics) | Высокая | Средний | Задокументировать как known limitation; стабилизировать что можно (фикс seed где backend позволяет, постпроцесс SVG для нормализации id); определять diagrams как «best-effort determinism» в отличие от core/viz. |
| matplotlib/freetype-версии дрейфуют → разные PNG-байты | Средняя | Средний | Жёсткий пин в `requirements.d/viz.txt` (точные minor); хэш req включает тиры → детерминированный venv. |
| Extract OMML в модуль ломает существующие `r.formula` | Низкая | Высокий | BC-shim `Report.formula → self.math.formula`; math auto-attach; перенос `_omml.py` verbatim без правки логики. |
| Раздувание базового скилла (vendored viz = +66 МБ когда-нибудь по умолчанию) | Низкая | Средний | Код модуля vendored (мал), но deps НЕ ставятся пока не позван `r.plot`; lightweight default неизменен. |
| Third-party pip-модуль с дублирующим namespace перетирает core | Низкая | Средний | `discover()`: built-in приоритетнее (`setdefault`), entry-points не могут переопределить vendored namespace. |
| Codex target не видит entry-points → «модуль не работает» у pip-юзеров на Codex | Средняя | Низкий | Документировать: на Codex/skills-only доступны только vendored-модули; third-party pip-модули требуют pip-окружения. |
| `__getattr__` + lazy attach скрывает ошибки импорта модуля как `AttributeError` | Средняя | Средний | В `_attach` пробрасываем `check_available()` исключение наружу как есть (ActionableImportError), не глотаем. |

---

## 11. ADR-009: Принять архитектуру core+modules (plugin)

### Status
Proposed (продолжает линию ADR-008 о глобальном state-dir).

### Context
`gost_report.py` — монолит 2086 строк. Владелец хочет «LaTeX-of-docx»: маленькое ядро + много opt-in модулей (viz, diagrams, bib, code, …), которые штампуются по шаблону и выглядят одинаково. Жёсткие силы: единый ГОСТ-счётчик «Рисунок N» для ВСЕХ визуалов; python-docx встраивает только растр; byte-determinism; дистрибуция папкой-скиллом без pip (и на Claude, и на Codex target); желание открыть дорогу third-party pip-пакетам `gost-report-*`.

### Decision
Мы распилим `gost_report.py` по уже существующим швам в пакет `gost_report/` (core) + отдельные пакеты-модули. Модули реализуют `Protocol ReportModule` и вешаются на `Report` через namespaced lazy-attach (`r.plot.line`, `r.diagram`). Shared core services (`embed_figure`/`embed_table`/counters/`printable_cm`/`prose.sanitize`/`assets`) предоставляются модулям через фасад `CoreServices`; модуль НИКОГДА не пишет подпись/нумерацию сам — производит PNG и зовёт `embed_figure`. Discovery двухканальный: явный `_BUILTIN` для vendored + `importlib.metadata` entry points для pip. Тиры зависимостей — `requirements.d/<extra>.txt` + extras в `pyproject`, активируемые через `GOST_REPORT_EXTRAS` в `ensure_env.py`. Lightweight default (`python-docx`+`latex2mathml`) неизменен. OMML-пайплайн извлекается первым как модуль-образец `math` с BC-shim `Report.formula`.

### Consequences
- **Positive:** новый модуль = одна папка + строка в реестре (`new_module.py` штампует). Единый счётчик гарантирован типами (`embed_figure` — единственная точка нумерации). Lightweight default не тащит matplotlib. Vendored и pip-миры сосуществуют. BC для пользователей — ноль (re-export + shim). Codex target работает без изменений installer.
- **Negative:** `__getattr__`-магия добавляет одну неочевидную точку (документируется). Determinism у diagrams остаётся best-effort. Появляется `pyproject.toml`, не используемый в папке-скилле (дубль источника пинов с `requirements.d/`). Извлечение OMML — разовый риск регрессии формул (снимается shim + verbatim-переносом).

### Alternatives Considered
- **Оставить монолит, добавлять методы в `Report`.** Отклонено: flat-API даёт коллизии имён в экосистеме; каждый новый визуал-модуль тащит свои deps в base; нет шаблона «штамповать».
- **Полный pip-rewrite (выкинуть папку-скилл).** Отклонено: ломает текущую verbatim-дистрибуцию, Codex target, ADR-008 venv-модель; требует от пользователя pip-окружение, которого скилл специально избегает.
- **Только entry-points (всё через pip, без `_BUILTIN`).** Отклонено: не работает в папке-скилле без pip и на Codex; viz/diagrams должны быть доступны из коробки.
- **ABC вместо Protocol.** Отклонено: заставляет third-party импортировать наш базовый класс (coupling); structural typing достаточно и дружелюбнее к pip-экосистеме.

---

## 12. Evolution path

- **Сейчас:** seam-extract (math out, CoreServices facade, registry) — ~1 неделя, ноль изменений для пользователя.
- **Далее:** viz + diagrams как vendored (исследование готово). Доказывает «единый счётчик через embed_figure» на двух независимых модулях.
- **Затем:** code/data/bib/xref — расширяют экосистему без правок core.
- **Appendix (#10)** триггерит единственное расширение core: namespaced counters («Рисунок А.1»). К этому моменту seam проверен на 5+ модулях — расширять безопасно.
- **Third-party:** первый внешний `gost-report-chem` через entry-points валидирует pip-канал. Если их станет много — публикуем сам core в PyPI и держим папку-скилл как «batteries-included» срез.

---

## Приложение. Исследовательская база (решения, вшитые в дизайн)

**viz (matplotlib):** PNG @ 300 dpi; шрифт Liberation Serif (SIL OFL, метрически совместим с Times New Roman) с фолбэк-цепочкой `['Liberation Serif','Times New Roman','PT Serif','DejaVu Serif']`, `mathtext.fontset='stix'`; палитра Okabe-Ito `['#E69F00','#56B4E9','#009E73','#F0E442','#0072B2','#D55E00','#CC79A7','#000000']` + второй канал (linestyle/marker/hatch) для grayscale; determinism через `savefig(metadata={'Software':None})` + пины версий; один `apply_gost_style()` rcParams-пресет; первые хелперы line/scatter/bar/grouped_bar/histogram. Тир `[viz]` = только numpy+matplotlib (~66 МБ); scipy/pandas наружу.

**diagrams (mermaid):** рендер в PNG через pluggable backend — дефолт `merm`/`mermaid-py` (pure-python, fidelity-оговорки), opt-in `mmdc` (Node+Chromium, точно), `mermaid.ink`/kroki только явным сетевым фолбэком; SVG→PNG @300dpi; ГОСТ-тема (neutral+grayscale+Liberation Serif) инжектом `%%{init:...}%%`; determinism — best-effort (random SVG ids). Тир `[diagrams]`.

Источники: ГОСТ 7.32-2017 §6.5 (иллюстрации); Paul Tol Colour Schemes (SRON/EPS/TN/09-002); Okabe & Ito, Color Universal Design (jfly.uni-koeln.de/color); Liberation fonts (SIL OFL 1.1); matplotlib savefig metadata docs; python-docx (растр-only embedding).
