"""core_api — контракты подключаемых модулей gost-report («LaTeX-of-docx»).

Маленькое стабильное ядро (`Report`) + много opt-in модулей (viz, diagrams, …),
которые вешаются на Report через namespaced lazy-attach (`r.plot.line(...)`,
`r.diagram(...)`). Модуль НИКОГДА не пишет подпись/нумерацию сам — он производит
PNG и зовёт shared core service `core.embed_figure(png, caption)`. Благодаря
этому matplotlib-график, graphviz-диаграмма и готовый PNG делят ОДИН сквозной
счётчик «Рисунок N — …» (требование ГОСТ 7.32 §6.5).

Контракты ниже — `typing.Protocol` (структурная типизация): сторонний pip-модуль
НЕ обязан импортировать наши классы, достаточно совпадения формы. Это снижает
сцепление и даёт дорогу экосистеме `gost-report-<module>`.

Зависит только от stdlib — грузится в lightweight-дефолте без тяжёлых пакетов.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol, Sequence, runtime_checkable


# ----------------------------------------------------------------------------
# Actionable import error — внятная команда-починка вместо голого ModuleNotFound
# ----------------------------------------------------------------------------
class ActionableImportError(ImportError):
    """ImportError с конкретной инструкцией, привязанной к тиру зависимостей."""

    @classmethod
    def for_extra(cls, module_title: str, extra: str,
                  missing: str) -> "ActionableImportError":
        return cls(
            f"{module_title}: не хватает пакета '{missing}' (тир '{extra}').\n"
            f"  Через ensure_env.py (рекомендуется):\n"
            f"      GOST_REPORT_EXTRAS={extra} python3 scripts/ensure_env.py\n"
            f"  Или вручную в активный Python:\n"
            f"      pip install {missing}\n"
            f"  Pip-режим пакета: pip install 'gost-report[{extra}]'"
        )


# ----------------------------------------------------------------------------
# Figure — всё, что встраивается как «иллюстрация» по ГОСТ
# ----------------------------------------------------------------------------
@runtime_checkable
class Figure(Protocol):
    """Встраиваемый визуал. `render()` детерминированно кладёт PNG на диск и
    возвращает путь; нумерацию и подпись делает core (`embed_figure`), не объект.
    """

    def render(self, *, dpi: int = 300, max_width_cm: float = 16.0) -> Path: ...

    @property
    def natural_width_cm(self) -> Optional[float]: ...


def is_figure_like(obj: object) -> bool:
    """Duck-typed проверка Figure без импорта тяжёлых модулей и без isinstance
    по Protocol (тот требует runtime_checkable и медленнее). Путь — НЕ Figure."""
    return (
        not isinstance(obj, (str, Path))
        and callable(getattr(obj, "render", None))
    )


# ----------------------------------------------------------------------------
# CoreServices — что ядро отдаёт модулю (модуль это ТОЛЬКО потребляет)
# ----------------------------------------------------------------------------
@runtime_checkable
class CoreServices(Protocol):
    """Shared-сервисы ядра. Модуль не трогает нумерацию/подписи/determinism
    напрямую — он зовёт сюда. `Report` реализует этот протокол структурно."""

    # единый embed + единый счётчик рисунков/таблиц
    def embed_figure(self, image, caption: str, *,
                     width_cm: Optional[float] = None) -> int: ...

    def embed_table(self, rows: Sequence[Sequence[str]], caption: str = "",
                    *, has_header: bool = True) -> int: ...

    # геометрия / контекст
    def printable_cm(self) -> float: ...

    # прозовый контент (для bib/code/glossary) и санитайз подписей
    def sanitize(self, text: str) -> str: ...

    # временная директория модуля (чистится в teardown ядра)
    @property
    def tmp_dir(self) -> Path: ...

    # доступ к профилю/путям/документу (escape hatch — использовать редко)
    @property
    def profile(self): ...

    @property
    def paths(self): ...

    @property
    def doc(self): ...


# ----------------------------------------------------------------------------
# ReportModule — контракт расширения
# ----------------------------------------------------------------------------
@runtime_checkable
class ReportModule(Protocol):
    """Жизненный цикл: construct → check_available → attach(core) →
    (использование через r.<namespace>) → teardown."""

    #: имя атрибута на Report: r.<namespace> ("plot", "diagram", "bib", ...)
    namespace: str
    #: человекочитаемое имя для сообщений об ошибках
    title: str
    #: ключ тира зависимостей ("viz", "diagrams", ...) или None для default-тира
    requires_extra: Optional[str]

    def check_available(self) -> None:
        """Бросает ActionableImportError, если deps нет. Вызывается лениво при
        ПЕРВОМ обращении к r.<namespace>, не при attach — чтобы документ без
        графиков не требовал matplotlib."""
        ...

    def attach(self, core: "CoreServices"):
        """Возвращает объект, который станет r.<namespace> (его API)."""
        ...

    def teardown(self) -> None:
        """Идемпотентная чистка (tmp-файлы, открытые figure-объекты)."""
        ...
