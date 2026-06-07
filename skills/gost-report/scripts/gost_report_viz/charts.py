"""Figure-объекты viz. Каждый реализует протокол core_api.Figure:
render() детерминированно пишет PNG и возвращает путь; нумерацию/подпись
делает ядро через embed_figure.

PNG детерминирован: имя по sha256 содержимого, savefig(metadata={'Software':None}),
без таймстемпов. Байт-в-байт стабильность — при фиксированных версиях
matplotlib/freetype (см. research/19, тир [viz]).

Кросс-режущие параметры (hlines/vlines/ylim/xlim/yscale/annotations) живут на
базовом _Chart и применяются обобщённо в render() — каждый тип графика получает
их бесплатно. Детерминизм: см. _base_key() ниже.
"""
from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Union

from .style import (
    REFLINE_COLOR, REFLINE_LINESTYLE, REFLINE_LINEWIDTH,
    apply_gost_style, second_channel, series_color, series_hatch,
)

Number = Union[int, float]
Series = Sequence[Number]

_LINE_KEYS = ("value", "label", "color", "linestyle", "linewidth")
_ANNO_KEYS = ("x", "y", "text", "dx", "dy", "arrow", "color", "ha", "va")


def _as_series(y) -> List[List[float]]:
    """Нормализует y к списку серий. Плоская последовательность чисел → одна
    серия; последовательность последовательностей → несколько серий."""
    seq = list(y)
    first_is_seq = bool(seq) and (
        isinstance(seq[0], (list, tuple))
        or (hasattr(seq[0], "__iter__") and not isinstance(seq[0], (str, bytes)))
    )
    if first_is_seq:
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


def norm_lines(items) -> List[dict]:
    """Нормализует hlines/vlines: число или dict → полный dict с дефолтами.
    Чистая детерминированная функция; вызывается и в module.py (при сборке
    графика), и в _base_key (для хэша) — одинаковая картинка → одинаковый PNG."""
    out: List[dict] = []
    for it in items or []:
        if isinstance(it, dict):
            d = it
        else:
            d = {"value": it}
        out.append({
            "value": float(d["value"]),
            "label": d.get("label"),
            "color": d.get("color"),
            "linestyle": d.get("linestyle"),
            "linewidth": d.get("linewidth"),
        })
    return out


def norm_annotations(items) -> List[dict]:
    """Нормализует annotations к полным dict'ам с дефолтами."""
    out: List[dict] = []
    for it in items or []:
        out.append({
            "x": float(it["x"]),
            "y": float(it["y"]),
            "text": str(it["text"]),
            "dx": float(it.get("dx", 6.0)),
            "dy": float(it.get("dy", 6.0)),
            "arrow": bool(it.get("arrow", False)),
            "color": it.get("color"),
            "ha": it.get("ha"),
            "va": it.get("va"),
        })
    return out


def _key_lines(items) -> tuple:
    """Стабильный, порядко-независимый по ключам repr-кортеж для hlines/vlines."""
    return tuple(tuple((k, d[k]) for k in _LINE_KEYS) for d in items)


def _key_annos(items) -> tuple:
    return tuple(tuple((k, d[k]) for k in _ANNO_KEYS) for d in items)


def _fmt_value(v: float) -> str:
    """Детерминированный авто-формат значения для value_labels.
    Целое → int; иначе до 3 значащих знаков, хвостовые нули срезаются.
    Без locale, без платформо-зависимого округления."""
    fv = float(v)
    if fv == int(fv):
        return str(int(fv))
    s = f"{fv:.3g}"
    return s


def _apply_colors(default_color, colors, i):
    """Escape hatch: явный colors[i] (с цикличностью) поверх детерминированного
    дефолта. None → дефолт (старые хэши стабильны)."""
    if colors:
        return colors[i % len(colors)]
    return default_color


@dataclass
class _Chart:
    xlabel: str = ""
    ylabel: str = ""
    natural_width_cm: float = 16.0
    aspect: float = 0.62          # высота/ширина
    # --- кросс-режущие поля (рисуются/применяются в render()) ---
    hlines: List[dict] = field(default_factory=list)
    vlines: List[dict] = field(default_factory=list)
    ylim: Optional[tuple] = None
    xlim: Optional[tuple] = None
    yscale: str = "linear"
    annotations: List[dict] = field(default_factory=list)
    _out_dir: Optional[Path] = field(default=None, init=False, repr=False)

    # --- subclass hooks ---
    def _content_key(self) -> str:
        raise NotImplementedError

    def _draw(self, ax) -> None:
        raise NotImplementedError

    # --- ДЕТЕРМИНИЗМ-КОНТРАКТ ---
    # Все общие (кросс-режущие) поля сворачиваются в хэш РОВНО здесь, один раз.
    # Каждый подкласс возвращает _base_key() + repr(<свой кортеж>), поэтому
    # структурно невозможно добавить общее поле, которое утечёт мимо хэша.
    def _base_key(self) -> str:
        return repr((
            _key_lines(self.hlines), _key_lines(self.vlines),
            self.ylim, self.xlim, self.yscale,
            _key_annos(self.annotations),
        ))

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
            # порядок: scale → limits → reflines → annotations → legend
            if self.yscale and self.yscale != "linear":
                ax.set_yscale(self.yscale)
            if self.xlim is not None:
                ax.set_xlim(*self.xlim)
            if self.ylim is not None:
                ax.set_ylim(*self.ylim)
            self._draw_reflines(ax)
            self._draw_annotations(ax)
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

    # --- обобщённая отрисовка опорных линий и аннотаций ---
    def _draw_reflines(self, ax) -> None:
        for d in self.hlines:
            ax.axhline(
                d["value"], color=d["color"] or REFLINE_COLOR,
                linestyle=d["linestyle"] or REFLINE_LINESTYLE,
                linewidth=d["linewidth"] or REFLINE_LINEWIDTH,
                label=d["label"],
            )
        for d in self.vlines:
            ax.axvline(
                d["value"], color=d["color"] or REFLINE_COLOR,
                linestyle=d["linestyle"] or REFLINE_LINESTYLE,
                linewidth=d["linewidth"] or REFLINE_LINEWIDTH,
                label=d["label"],
            )

    def _draw_annotations(self, ax) -> None:
        for d in self.annotations:
            kw = dict(
                xy=(d["x"], d["y"]), textcoords="offset points",
                xytext=(d["dx"], d["dy"]), fontsize=9,
                color=d["color"] or "black",
            )
            if d["ha"]:
                kw["ha"] = d["ha"]
            if d["va"]:
                kw["va"] = d["va"]
            if d["arrow"]:
                kw["arrowprops"] = {"arrowstyle": "->", "color": d["color"] or "black"}
            ax.annotate(d["text"], **kw)


@dataclass
class LineChart(_Chart):
    x: Series = field(default_factory=list)
    series: List[List[float]] = field(default_factory=list)
    labels: List[Optional[str]] = field(default_factory=list)
    colors: Optional[List[str]] = None
    linestyles: Optional[List[str]] = None
    markers: Union[List[str], bool, None] = None

    def _content_key(self) -> str:
        return self._base_key() + "|" + repr((
            [float(v) for v in self.x], self.series, self.labels,
            self.colors, self.linestyles, self.markers,
        ))

    def _draw(self, ax) -> None:
        x = list(self.x)
        me = _markevery(len(x))
        for i, ys in enumerate(self.series):
            kw = dict(second_channel(i))
            if self.colors:
                kw["color"] = self.colors[i % len(self.colors)]
            if self.linestyles:
                kw["linestyle"] = self.linestyles[i % len(self.linestyles)]
            if self.markers is False:
                kw["marker"] = "None"
                me = None
            elif isinstance(self.markers, (list, tuple)) and self.markers:
                kw["marker"] = self.markers[i % len(self.markers)]
            # markers is None или True → детерминированный маркер серии (вкл. по умолчанию)
            ax.plot(x, ys, markevery=me, label=self.labels[i], **kw)


@dataclass
class ScatterChart(_Chart):
    x: Series = field(default_factory=list)
    series: List[List[float]] = field(default_factory=list)
    labels: List[Optional[str]] = field(default_factory=list)
    colors: Optional[List[str]] = None
    markers: Union[List[str], None] = None

    def _content_key(self) -> str:
        return self._base_key() + "|" + repr((
            [float(v) for v in self.x], self.series, self.labels,
            self.colors, self.markers,
        ))

    def _draw(self, ax) -> None:
        from .style import MARKERS
        x = list(self.x)
        for i, ys in enumerate(self.series):
            color = _apply_colors(series_color(i), self.colors, i)
            if self.markers:
                marker = self.markers[i % len(self.markers)]
            else:
                marker = MARKERS[i % len(MARKERS)]
            ax.scatter(x, ys, color=color, marker=marker,
                       edgecolors="black", linewidths=0.4,
                       label=self.labels[i])


@dataclass
class BarChart(_Chart):
    categories: List[str] = field(default_factory=list)
    values: List[float] = field(default_factory=list)
    value_labels: bool = False
    value_fmt: Optional[str] = None
    colors: Optional[List[str]] = None
    horizontal: bool = False

    def _content_key(self) -> str:
        return self._base_key() + "|" + repr((
            self.categories, self.values, self.value_labels,
            self.value_fmt, self.colors, self.horizontal,
        ))

    def _draw(self, ax) -> None:
        vals = [float(v) for v in self.values]
        colors = [_apply_colors(series_color(i), self.colors, i)
                  for i in range(len(vals))]
        if self.horizontal:
            container = ax.barh(list(self.categories), vals,
                                color=colors, edgecolor="black", linewidth=0.6)
            ax.grid(axis="y", visible=False)
        else:
            container = ax.bar(list(self.categories), vals,
                               color=colors, edgecolor="black", linewidth=0.6)
            ax.grid(axis="x", visible=False)
        if self.value_labels:
            labels = [(self.value_fmt.format(v) if self.value_fmt else _fmt_value(v))
                      for v in vals]
            ax.bar_label(container, labels=labels, padding=2, fontsize=9)


@dataclass
class GroupedBarChart(_Chart):
    categories: List[str] = field(default_factory=list)
    series: List[List[float]] = field(default_factory=list)
    labels: List[Optional[str]] = field(default_factory=list)
    value_labels: bool = False
    value_fmt: Optional[str] = None
    colors: Optional[List[str]] = None
    horizontal: bool = False

    def _content_key(self) -> str:
        return self._base_key() + "|" + repr((
            self.categories, self.series, self.labels, self.value_labels,
            self.value_fmt, self.colors, self.horizontal,
        ))

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
            color = _apply_colors(series_color(i), self.colors, i)
            vals = [float(v) for v in ys]
            pos = [p + offset for p in positions]
            if self.horizontal:
                container = ax.barh(pos, vals, height=bw, color=color,
                                    hatch=series_hatch(i), edgecolor="black",
                                    linewidth=0.6, label=self.labels[i])
            else:
                container = ax.bar(pos, vals, width=bw, color=color,
                                   hatch=series_hatch(i), edgecolor="black",
                                   linewidth=0.6, label=self.labels[i])
            if self.value_labels:
                lbls = [(self.value_fmt.format(v) if self.value_fmt else _fmt_value(v))
                        for v in vals]
                ax.bar_label(container, labels=lbls, padding=2, fontsize=8)
        if self.horizontal:
            ax.set_yticks(positions)
            ax.set_yticklabels(list(self.categories))
            ax.grid(axis="y", visible=False)
        else:
            ax.set_xticks(positions)
            ax.set_xticklabels(list(self.categories))
            ax.grid(axis="x", visible=False)


@dataclass
class StackedBarChart(_Chart):
    categories: List[str] = field(default_factory=list)
    series: List[List[float]] = field(default_factory=list)
    labels: List[Optional[str]] = field(default_factory=list)
    value_labels: bool = False
    value_fmt: Optional[str] = None
    colors: Optional[List[str]] = None

    def _content_key(self) -> str:
        return self._base_key() + "|" + repr((
            self.categories, self.series, self.labels, self.value_labels,
            self.value_fmt, self.colors,
        ))

    def _draw(self, ax) -> None:
        n_groups = len(self.categories)
        if not self.series:
            return
        positions = list(range(n_groups))
        bottoms = [0.0] * n_groups
        for i, ys in enumerate(self.series):
            vals = [float(v) for v in ys]
            color = _apply_colors(series_color(i), self.colors, i)
            container = ax.bar(positions, vals, bottom=bottoms, width=0.6,
                               color=color, hatch=series_hatch(i),
                               edgecolor="black", linewidth=0.6,
                               label=self.labels[i])
            if self.value_labels:
                lbls = [(self.value_fmt.format(v) if self.value_fmt else _fmt_value(v))
                        for v in vals]
                ax.bar_label(container, labels=lbls, label_type="center", fontsize=8)
            bottoms = [b + v for b, v in zip(bottoms, vals)]
        ax.set_xticks(positions)
        ax.set_xticklabels(list(self.categories))
        ax.grid(axis="x", visible=False)


@dataclass
class AreaChart(_Chart):
    x: Series = field(default_factory=list)
    series: List[List[float]] = field(default_factory=list)
    labels: List[Optional[str]] = field(default_factory=list)
    stacked: bool = False
    colors: Optional[List[str]] = None

    def _content_key(self) -> str:
        return self._base_key() + "|" + repr((
            [float(v) for v in self.x], self.series, self.labels,
            self.stacked, self.colors,
        ))

    def _draw(self, ax) -> None:
        x = list(self.x)
        if self.stacked:
            colors = [_apply_colors(series_color(i), self.colors, i)
                      for i in range(len(self.series))]
            ax.stackplot(x, *self.series, labels=self.labels, colors=colors,
                         edgecolor="black", linewidth=0.6)
        else:
            for i, ys in enumerate(self.series):
                color = _apply_colors(series_color(i), self.colors, i)
                ax.fill_between(x, ys, color=color, alpha=0.5,
                                hatch=series_hatch(i) or None,
                                edgecolor="black", linewidth=0.8,
                                label=self.labels[i])


@dataclass
class Histogram(_Chart):
    data: List[float] = field(default_factory=list)
    bins: int = 20

    def _content_key(self) -> str:
        return self._base_key() + "|" + repr((
            [float(v) for v in self.data], self.bins))

    def _draw(self, ax) -> None:
        ax.hist([float(v) for v in self.data], bins=self.bins,
                color=series_color(0), edgecolor="black", linewidth=0.6)
        ax.grid(axis="x", visible=False)
