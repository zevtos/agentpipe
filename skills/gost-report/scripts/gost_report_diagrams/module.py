"""DiagramModule — callable namespace `r.diagram`. Диаграммы через Graphviz (DOT).

    r.diagram("digraph { A -> B -> C }", caption="Схема обработки")   # встроить → №
    fig = r.diagram("digraph { A -> B }")                              # вернуть Figure

Покрывает структурные схемы, блок-схемы алгоритмов (ГОСТ 19.701), деревья, ER,
графы зависимостей. Красивое оформление по ГОСТ инжектится автоматически
(светло-серые скруглённые блоки, тёмная рамка, чёрный текст, serif-шрифт под
Times New Roman) — пользовательские атрибуты в DOT переопределяют дефолты.

Зависимость — только системный `dot` (brew/apt install graphviz). PNG напрямую,
без растеризатора и без Node. Детерминизм — best-effort (зависит от версии dot).
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Optional

from .backends import DiagramError, available, render_dot, _INSTALL_HINT

# ГОСТ-дефолты: монохром-дружелюбно (читается в Ч/Б), serif под основной текст.
DEFAULT_FONT = "Times New Roman"


def _defaults_block(font: str, rankdir: Optional[str]) -> str:
    graph_attrs = (
        f'bgcolor="white" fontname="{font}" fontsize=12 '
        f'nodesep=0.4 ranksep=0.5 pad=0.2 splines=true'
    )
    if rankdir:
        graph_attrs += f' rankdir="{rankdir}"'
    return (
        f'  graph [{graph_attrs}];\n'
        f'  node [shape=box style="rounded,filled" fillcolor="#f5f5f5" '
        f'color="#333333" fontname="{font}" fontsize=12 penwidth=1.0 '
        f'margin="0.15,0.08"];\n'
        f'  edge [color="#333333" fontname="{font}" fontsize=11 '
        f'penwidth=1.0 arrowsize=0.8];\n'
    )


def _inject(source: str, *, font: str, rankdir: Optional[str]) -> str:
    """Вставляет ГОСТ-дефолты сразу после открывающей «{» (пользовательские
    атрибуты, идущие дальше, переопределяют их). Если «{» нет — оборачивает
    тело в digraph."""
    defaults = _defaults_block(font, rankdir)
    s = source.strip()
    brace = s.find("{")
    if brace == -1:
        return "digraph G {\n" + defaults + s + "\n}\n"
    return s[:brace + 1] + "\n" + defaults + s[brace + 1:]


class _DiagramFigure:
    natural_width_cm = None      # реальный размер берётся из PNG, кламп в figure()

    def __init__(self, dot_source: str, *, out_dir: Path, dpi: int, engine: str):
        self._src = dot_source
        self._out_dir = out_dir
        self._dpi = dpi
        self._engine = engine

    def render(self, *, dpi: int = 300, max_width_cm: float = 16.0) -> Path:
        key = hashlib.sha256(
            (self._src + self._engine).encode("utf-8")).hexdigest()[:16]
        out_dir = self._out_dir or Path(tempfile.gettempdir())
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"diagram_{key}.png"
        if out.exists():
            return out
        render_dot(self._src, out, dpi=self._dpi or dpi, engine=self._engine)
        return out


class _DiagramAPI:
    def __init__(self, core):
        self._core = core
        self._font = DEFAULT_FONT
        self._engine = "dot"
        self._dpi = 300

    def __call__(self, source, caption: Optional[str] = None, *,
                 engine: Optional[str] = None, font: Optional[str] = None,
                 rankdir: Optional[str] = None, style: bool = True,
                 width_cm: Optional[float] = None):
        # graphviz.Digraph / pydot объекты — берём .source / .to_string().
        if hasattr(source, "source"):
            source = source.source
        elif hasattr(source, "to_string"):
            source = source.to_string()
        dot = _inject(source, font=font or self._font, rankdir=rankdir) \
            if style else str(source)
        fig = _DiagramFigure(dot, out_dir=self._core.tmp_dir,
                             dpi=self._dpi, engine=engine or self._engine)
        if caption is None:
            return fig
        return self._core.embed_figure(fig, caption, width_cm=width_cm)

    def font(self, name: str) -> None:
        """Шрифт по умолчанию для последующих диаграмм."""
        self._font = name

    def engine(self, name: str) -> None:
        """Движок раскладки: dot|neato|fdp|sfdp|circo|twopi."""
        self._engine = name


class DiagramModule:
    namespace = "diagram"
    title = "Диаграммы (Graphviz/DOT → PNG)"
    requires_extra = None          # системный `dot`, не pip-тир

    def check_available(self) -> None:
        if not available():
            raise ImportError(_INSTALL_HINT)

    def attach(self, core):
        self.check_available()
        return _DiagramAPI(core)

    def teardown(self) -> None:
        pass
