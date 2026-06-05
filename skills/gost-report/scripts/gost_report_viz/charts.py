"""Figure-объекты viz. Каждый реализует протокол core_api.Figure:
render() детерминированно пишет PNG и возвращает путь; нумерацию/подпись
делает ядро через embed_figure.

PNG детерминирован: имя по sha256 содержимого, savefig(metadata={'Software':None}),
без таймстемпов. Байт-в-байт стабильность — при фиксированных версиях
matplotlib/freetype (см. research/19, тир [viz]).
"""
from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Union

from .style import apply_gost_style, second_channel, series_color, series_hatch

Number = Union[int, float]
Series = Sequence[Number]


def _as_series(y) -> List[List[float]]:
    """Нормализует y к списку серий. Плоская последовательность чисел → одна
    серия; последовательность последовательностей → несколько серий."""
    seq = list(y)
    if seq and isinstance(seq[0], (list, tuple)) or (
            seq and hasattr(seq[0], "__iter__") and not isinstance(seq[0], (str, bytes))):
        return [[float(v) for v in s] for s in seq]
    return [[float(v) for v in seq]]


def _labels_for(labels, n) -> List[Optional[str]]:
    labels = list(labels or [])
    return [labels[i] if i < len(labels) else None for i in range(n)]


def _markevery(n: int) -> Optional[int]:
    """Прореживание маркеров, чтобы линия из сотен точек не превращалась в кашу."""
    if n <= 16:
        return 1
    return max(1, n // 12)


@dataclass
class _Chart:
    xlabel: str = ""
    ylabel: str = ""
    natural_width_cm: float = 16.0
    aspect: float = 0.62          # высота/ширина
    _out_dir: Optional[Path] = field(default=None, init=False, repr=False)

    # --- subclass hooks ---
    def _content_key(self) -> str:
        raise NotImplementedError

    def _draw(self, ax) -> None:
        raise NotImplementedError

    # --- общий рендер ---
    def render(self, *, dpi: int = 300, max_width_cm: float = 16.0) -> Path:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        apply_gost_style()
        width_cm = min(self.natural_width_cm, max_width_cm)
        w_in = width_cm / 2.54
        fig, ax = plt.subplots(figsize=(w_in, w_in * self.aspect), dpi=dpi)
        try:
            self._draw(ax)
            if self.xlabel:
                ax.set_xlabel(self.xlabel)
            if self.ylabel:
                ax.set_ylabel(self.ylabel)
            _, lbls = ax.get_legend_handles_labels()
            if any(lbls):
                ax.legend()
            key = hashlib.sha256("|".join([
                type(self).__name__, self._content_key(),
                self.xlabel, self.ylabel, str(dpi), f"{width_cm:.3f}",
            ]).encode("utf-8")).hexdigest()[:16]
            out_dir = self._out_dir or Path(tempfile.gettempdir())
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"viz_{key}.png"
            fig.savefig(out, dpi=dpi, metadata={"Software": None})
            return out
        finally:
            plt.close(fig)


@dataclass
class LineChart(_Chart):
    x: Series = field(default_factory=list)
    series: List[List[float]] = field(default_factory=list)
    labels: List[Optional[str]] = field(default_factory=list)

    def _content_key(self) -> str:
        return repr((list(self.x), self.series, self.labels))

    def _draw(self, ax) -> None:
        x = list(self.x)
        me = _markevery(len(x))
        for i, ys in enumerate(self.series):
            ax.plot(x, ys, markevery=me, label=self.labels[i], **second_channel(i))


@dataclass
class ScatterChart(_Chart):
    x: Series = field(default_factory=list)
    series: List[List[float]] = field(default_factory=list)
    labels: List[Optional[str]] = field(default_factory=list)

    def _content_key(self) -> str:
        return repr((list(self.x), self.series, self.labels))

    def _draw(self, ax) -> None:
        from .style import MARKERS
        x = list(self.x)
        for i, ys in enumerate(self.series):
            ax.scatter(x, ys, color=series_color(i),
                       marker=MARKERS[i % len(MARKERS)],
                       edgecolors="black", linewidths=0.4,
                       label=self.labels[i])


@dataclass
class BarChart(_Chart):
    categories: List[str] = field(default_factory=list)
    values: List[float] = field(default_factory=list)

    def _content_key(self) -> str:
        return repr((self.categories, self.values))

    def _draw(self, ax) -> None:
        colors = [series_color(i) for i in range(len(self.values))]
        ax.bar(list(self.categories), [float(v) for v in self.values],
               color=colors, edgecolor="black", linewidth=0.6)
        ax.grid(axis="x", visible=False)


@dataclass
class GroupedBarChart(_Chart):
    categories: List[str] = field(default_factory=list)
    series: List[List[float]] = field(default_factory=list)
    labels: List[Optional[str]] = field(default_factory=list)

    def _content_key(self) -> str:
        return repr((self.categories, self.series, self.labels))

    def _draw(self, ax) -> None:
        n_groups = len(self.categories)
        n_series = len(self.series)
        if n_series == 0:
            return
        total = 0.8
        bw = total / n_series
        positions = list(range(n_groups))
        for i, ys in enumerate(self.series):
            offset = -total / 2 + bw * (i + 0.5)
            ax.bar([p + offset for p in positions], [float(v) for v in ys],
                   width=bw, color=series_color(i), hatch=series_hatch(i),
                   edgecolor="black", linewidth=0.6, label=self.labels[i])
        ax.set_xticks(positions)
        ax.set_xticklabels(list(self.categories))
        ax.grid(axis="x", visible=False)


@dataclass
class Histogram(_Chart):
    data: List[float] = field(default_factory=list)
    bins: int = 20

    def _content_key(self) -> str:
        return repr((list(self.data), self.bins))

    def _draw(self, ax) -> None:
        ax.hist([float(v) for v in self.data], bins=self.bins,
                color=series_color(0), edgecolor="black", linewidth=0.6)
        ax.grid(axis="x", visible=False)
