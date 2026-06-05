"""DiagramModule — callable namespace `r.diagram`.

    r.diagram("graph LR; A-->B", caption="Схема пайплайна")   # встроить → номер
    fig = r.diagram("graph LR; A-->B")                          # вернуть Figure

ГОСТ-тема (neutral + grayscale + serif) инжектится в начало исходника, если её
там ещё нет. r.diagram.theme(...) переопределяет тему для последующих вызовов.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Optional

from core_api import ActionableImportError
from .backends import DiagramError, available_backends, render

# ГОСТ-тема: нейтральная, оттенки серого, serif-шрифт (консистентно с viz/телом).
GOST_THEME = (
    "%%{init: {'theme':'neutral','themeVariables':{"
    "'fontFamily':'Liberation Serif, Times New Roman, serif',"
    "'fontSize':'14px',"
    "'primaryColor':'#ffffff','primaryTextColor':'#000000',"
    "'primaryBorderColor':'#000000','lineColor':'#000000',"
    "'secondaryColor':'#f0f0f0','tertiaryColor':'#ffffff'}}}%%\n"
)


class _DiagramFigure:
    """Figure-протокол: render() → PNG через каскад бэкендов."""

    natural_width_cm = 16.0

    def __init__(self, src: str, *, out_dir: Path, scale: int = 3):
        self._src = src
        self._out_dir = out_dir
        self._scale = scale

    def render(self, *, dpi: int = 300, max_width_cm: float = 16.0) -> Path:
        key = hashlib.sha256(self._src.encode("utf-8")).hexdigest()[:16]
        out_dir = self._out_dir or Path(tempfile.gettempdir())
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"diagram_{key}.png"
        if out.exists():
            return out
        render(self._src, out, dpi=dpi, scale=self._scale)
        return out


class _DiagramAPI:
    def __init__(self, core):
        self._core = core
        self._theme = GOST_THEME
        self._scale = 3

    def __call__(self, src: str, caption: Optional[str] = None, *,
                 theme: bool = True, width_cm: Optional[float] = None):
        full = src
        if theme and "%%{init" not in src:
            full = self._theme + src
        fig = _DiagramFigure(full, out_dir=self._core.tmp_dir, scale=self._scale)
        if caption is None:
            return fig
        return self._core.embed_figure(fig, caption, width_cm=width_cm)

    def theme(self, directive: str) -> None:
        """Заменить инжектируемую тему (целиком %%{init:...}%% директива)."""
        self._theme = directive if directive.endswith("\n") else directive + "\n"

    def scale(self, factor: int) -> None:
        """Масштаб растеризации (для mmdc). Больше = чётче, тяжелее."""
        self._scale = int(factor)


class DiagramModule:
    namespace = "diagram"
    title = "Диаграммы (mermaid → PNG)"
    requires_extra = "diagrams"

    def check_available(self) -> None:
        if not available_backends():
            raise ActionableImportError.for_extra(
                self.title, self.requires_extra, "merm")

    def attach(self, core):
        self.check_available()
        return _DiagramAPI(core)

    def teardown(self) -> None:
        pass
