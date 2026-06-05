"""Рендер диаграмм через системный Graphviz (`dot`) → PNG.

Только системный бинарь `dot` (brew/apt install graphviz) — без pip-зависимостей
и без Node. `dot` отдаёт PNG напрямую, без отдельного растеризатора.
Питон-пакет `graphviz` опционален (для построения Digraph-объектов из кода).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional


class DiagramError(RuntimeError):
    pass


_INSTALL_HINT = (
    "Модуль диаграмм требует системный Graphviz (бинарь `dot`):\n"
    "  macOS:          brew install graphviz\n"
    "  Debian/Ubuntu:  sudo apt install graphviz\n"
    "  Windows:        choco install graphviz\n"
    "Опционально для построения графов из Python: pip install graphviz"
)


def dot_bin() -> Optional[str]:
    return shutil.which("dot")


def available() -> bool:
    return dot_bin() is not None


def render_dot(dot_source: str, out: Path, *, dpi: int = 300,
               engine: str = "dot") -> Path:
    """DOT-исходник → PNG в out. engine: dot|neato|fdp|sfdp|circo|twopi."""
    dot = dot_bin()
    if not dot:
        raise DiagramError(_INSTALL_HINT)
    src = out.with_suffix(".dot")
    src.write_text(dot_source, encoding="utf-8")
    cmd = [dot, f"-K{engine}", "-Tpng", f"-Gdpi={dpi}", "-o", str(out), str(src)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out.exists():
        raise DiagramError(
            f"graphviz dot failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return out
