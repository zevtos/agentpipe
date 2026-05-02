"""Project-root detection + conventional artefact paths for gost-report.

Цель: убрать `Path(__file__).parent / "figures"` бойлерплейт из user-скриптов.
Скрипт может лежать где угодно (рекомендуется `<project>/.claude/gost-report/build.py`),
а пути к figures/tables/out выводятся автоматически из конвенции `<root>/docs/...`.

Project root определяется обходом вверх от caller'а с проверкой маркеров:
    1. .git/                  (cloned-репы, самый надёжный)
    2. Makefile               (lab-конвенция)
    3. pyproject.toml         (Python-проекты)
    4. .claude/               (Claude Code-проекты, last resort)
    5. Path.cwd() + RuntimeWarning (fallback)

"Contains marker" not "is marker" — скрипт внутри `<project>/.claude/gost-report/`
безопасно проходит мимо `.claude/` и попадает в `<project>/`, у которого есть .git.
"""
from __future__ import annotations

import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_MARKERS = (".git", "Makefile", "pyproject.toml", ".claude")


@dataclass(frozen=True)
class ProjectPaths:
    """Иммутабельный snapshot путей проекта. Получается через paths().
    Не делает mkdir — за создание директорий отвечает Report.save()."""
    root: Path       # project root (где найден маркер)
    docs: Path       # root / "docs"
    figures: Path    # root / "docs" / "figures"
    tables: Path     # root / "docs" / "tables"
    out: Path        # root / "docs" / "report.docx"  — дефолт для Report.save()
    tex: Path        # root / "docs" / "report.tex"   — для смешанных docx/latex пайплайнов


def _find_root(start: Path) -> Optional[Path]:
    """Обход вверх от start с поиском первого маркера. Возвращает None, если
    ни один маркер не нашёлся вплоть до корня FS."""
    start = start.resolve()
    if start.is_file():
        start = start.parent
    for candidate in (start, *start.parents):
        for marker in _MARKERS:
            if (candidate / marker).exists():
                return candidate
    return None


def _caller_file() -> Optional[Path]:
    """__file__ ближайшего фрейма ВНЕ скилл-директории. Игнорируем не только
    _paths.py, но и весь scripts/ — иначе при вызове paths() из Report.__init__
    мы возьмём gost_report.py вместо user-скрипта."""
    frame = sys._getframe(1)
    skill_scripts_dir = str(Path(__file__).resolve().parent)
    while frame is not None:
        f = frame.f_globals.get("__file__")
        if f:
            f_resolved = str(Path(f).resolve())
            # Пропускаем любой файл внутри скилл-scripts/ (gost_report.py,
            # _paths.py, любые будущие helper-модули скилла).
            if not f_resolved.startswith(skill_scripts_dir + "/") and \
               f_resolved != skill_scripts_dir:
                return Path(f)
        frame = frame.f_back
    return None


def paths(start: Optional[Path] = None) -> ProjectPaths:
    """Вернуть ProjectPaths, рассчитанные от start (default: caller __file__).

    Если start не передан — инспектируем стек вызовов, берём __file__ ближайшего
    фрейма за пределами _paths.py / gost_report.py. Если и это не сработало —
    fallback на Path.cwd() с RuntimeWarning.

    Маркеры project root: .git → Makefile → pyproject.toml → .claude. First-match
    wins (ближайший проект, не самый внешний). Если ни один не найден,
    fallback к start (или cwd) с warning.
    """
    if start is None:
        start = _caller_file() or Path.cwd()
    start = Path(start)

    root = _find_root(start)
    if root is None:
        root = start.resolve() if start.is_dir() else start.resolve().parent
        warnings.warn(
            f"gost_report.paths(): no project marker ({', '.join(_MARKERS)}) "
            f"found above {start}; falling back to {root}",
            RuntimeWarning,
            stacklevel=2,
        )

    docs = root / "docs"
    return ProjectPaths(
        root=root,
        docs=docs,
        figures=docs / "figures",
        tables=docs / "tables",
        out=docs / "report.docx",
        tex=docs / "report.tex",
    )
