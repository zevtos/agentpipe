"""VizModule — namespace `r.plot`. Подключается лениво при первом обращении.

Эргономика: каждый хелпер либо встраивает рисунок (если передан caption) и
возвращает его номер (int), либо возвращает Figure-объект для ручного
r.figure(chart, caption). Один call-site для графика, диаграммы и готового PNG.
"""
from __future__ import annotations

from typing import Optional, Sequence

from core_api import ActionableImportError
from .charts import (
    AreaChart, BarChart, GroupedBarChart, Histogram, LineChart, ScatterChart,
    StackedBarChart, _as_series, _labels_for, norm_annotations, norm_lines,
)

_YSCALES = ("linear", "log")


def _check_yscale(yscale: str) -> str:
    if yscale not in _YSCALES:
        raise ValueError(
            f"yscale={yscale!r} не поддержан; допустимо: {', '.join(_YSCALES)}"
        )
    return yscale


class _VizAPI:
    def __init__(self, core):
        self._core = core

    # --- общий хвост: встроить или вернуть Figure ---
    def _finish(self, chart, caption: Optional[str], width_cm: Optional[float]):
        chart._out_dir = self._core.tmp_dir
        if caption is None:
            return chart
        return self._core.embed_figure(chart, caption, width_cm=width_cm)

    # --- общие кросс-режущие kwargs → нормализованные поля графика ---
    @staticmethod
    def _thread_shared(chart, *, hlines, vlines, ylim, xlim, yscale, annotations):
        chart.hlines = norm_lines(hlines)
        chart.vlines = norm_lines(vlines)
        chart.ylim = tuple(ylim) if ylim is not None else None
        chart.xlim = tuple(xlim) if xlim is not None else None
        chart.yscale = _check_yscale(yscale)
        chart.annotations = norm_annotations(annotations)
        return chart

    def line(self, x: Sequence, y, *, labels=None, xlabel: str = "",
             ylabel: str = "", caption: Optional[str] = None,
             width_cm: Optional[float] = None,
             hlines=None, vlines=None, ylim=None, xlim=None,
             yscale: str = "linear", annotations=None,
             colors=None, linestyles=None, markers=None):
        series = _as_series(y)
        chart = LineChart(xlabel=xlabel, ylabel=ylabel)
        chart.x = list(x)
        chart.series = series
        chart.labels = _labels_for(labels, len(series))
        chart.colors = list(colors) if colors is not None else None
        chart.linestyles = list(linestyles) if linestyles is not None else None
        chart.markers = (markers if isinstance(markers, bool)
                         else (list(markers) if markers is not None else None))
        self._thread_shared(chart, hlines=hlines, vlines=vlines, ylim=ylim,
                            xlim=xlim, yscale=yscale, annotations=annotations)
        return self._finish(chart, caption, width_cm)

    def scatter(self, x: Sequence, y, *, labels=None, xlabel: str = "",
                ylabel: str = "", caption: Optional[str] = None,
                width_cm: Optional[float] = None,
                hlines=None, vlines=None, ylim=None, xlim=None,
                yscale: str = "linear", annotations=None,
                colors=None, markers=None):
        series = _as_series(y)
        chart = ScatterChart(xlabel=xlabel, ylabel=ylabel)
        chart.x = list(x)
        chart.series = series
        chart.labels = _labels_for(labels, len(series))
        chart.colors = list(colors) if colors is not None else None
        chart.markers = list(markers) if markers is not None else None
        self._thread_shared(chart, hlines=hlines, vlines=vlines, ylim=ylim,
                            xlim=xlim, yscale=yscale, annotations=annotations)
        return self._finish(chart, caption, width_cm)

    def bar(self, categories: Sequence[str], values: Sequence, *,
            xlabel: str = "", ylabel: str = "", caption: Optional[str] = None,
            width_cm: Optional[float] = None,
            value_labels: bool = False, value_fmt: Optional[str] = None,
            colors=None, horizontal: bool = False,
            hlines=None, vlines=None, ylim=None, xlim=None,
            yscale: str = "linear", annotations=None):
        chart = BarChart(xlabel=xlabel, ylabel=ylabel)
        chart.categories = [str(c) for c in categories]
        chart.values = list(values)
        chart.value_labels = value_labels
        chart.value_fmt = value_fmt
        chart.colors = list(colors) if colors is not None else None
        chart.horizontal = horizontal
        self._thread_shared(chart, hlines=hlines, vlines=vlines, ylim=ylim,
                            xlim=xlim, yscale=yscale, annotations=annotations)
        return self._finish(chart, caption, width_cm)

    def grouped_bar(self, categories: Sequence[str], series, *, labels=None,
                    xlabel: str = "", ylabel: str = "",
                    caption: Optional[str] = None,
                    width_cm: Optional[float] = None,
                    value_labels: bool = False, value_fmt: Optional[str] = None,
                    colors=None, horizontal: bool = False,
                    hlines=None, vlines=None, ylim=None, xlim=None,
                    yscale: str = "linear", annotations=None):
        series = _as_series(series)
        chart = GroupedBarChart(xlabel=xlabel, ylabel=ylabel)
        chart.categories = [str(c) for c in categories]
        chart.series = series
        chart.labels = _labels_for(labels, len(series))
        chart.value_labels = value_labels
        chart.value_fmt = value_fmt
        chart.colors = list(colors) if colors is not None else None
        chart.horizontal = horizontal
        self._thread_shared(chart, hlines=hlines, vlines=vlines, ylim=ylim,
                            xlim=xlim, yscale=yscale, annotations=annotations)
        return self._finish(chart, caption, width_cm)

    def stacked_bar(self, categories: Sequence[str], series, *, labels=None,
                    xlabel: str = "", ylabel: str = "",
                    caption: Optional[str] = None,
                    width_cm: Optional[float] = None,
                    value_labels: bool = False, value_fmt: Optional[str] = None,
                    colors=None,
                    hlines=None, vlines=None, ylim=None, xlim=None,
                    yscale: str = "linear", annotations=None):
        series = _as_series(series)
        chart = StackedBarChart(xlabel=xlabel, ylabel=ylabel)
        chart.categories = [str(c) for c in categories]
        chart.series = series
        chart.labels = _labels_for(labels, len(series))
        chart.value_labels = value_labels
        chart.value_fmt = value_fmt
        chart.colors = list(colors) if colors is not None else None
        self._thread_shared(chart, hlines=hlines, vlines=vlines, ylim=ylim,
                            xlim=xlim, yscale=yscale, annotations=annotations)
        return self._finish(chart, caption, width_cm)

    def area(self, x: Sequence, y, *, labels=None, stacked: bool = False,
             xlabel: str = "", ylabel: str = "", caption: Optional[str] = None,
             width_cm: Optional[float] = None, colors=None,
             hlines=None, vlines=None, ylim=None, xlim=None,
             yscale: str = "linear", annotations=None):
        series = _as_series(y)
        chart = AreaChart(xlabel=xlabel, ylabel=ylabel)
        chart.x = list(x)
        chart.series = series
        chart.labels = _labels_for(labels, len(series))
        chart.stacked = stacked
        chart.colors = list(colors) if colors is not None else None
        self._thread_shared(chart, hlines=hlines, vlines=vlines, ylim=ylim,
                            xlim=xlim, yscale=yscale, annotations=annotations)
        return self._finish(chart, caption, width_cm)

    def histogram(self, data: Sequence, *, bins: int = 20, xlabel: str = "",
                  ylabel: str = "Частота", caption: Optional[str] = None,
                  width_cm: Optional[float] = None,
                  hlines=None, vlines=None, ylim=None, xlim=None,
                  yscale: str = "linear", annotations=None):
        chart = Histogram(xlabel=xlabel, ylabel=ylabel)
        chart.data = list(data)
        chart.bins = bins
        self._thread_shared(chart, hlines=hlines, vlines=vlines, ylim=ylim,
                            xlim=xlim, yscale=yscale, annotations=annotations)
        return self._finish(chart, caption, width_cm)


class VizModule:
    namespace = "plot"
    title = "Графики (matplotlib, ГОСТ-стиль)"
    requires_extra = "viz"

    def check_available(self) -> None:
        for pkg in ("matplotlib", "numpy"):
            try:
                __import__(pkg)
            except ImportError as e:
                raise ActionableImportError.for_extra(
                    self.title, self.requires_extra, pkg) from e

    def attach(self, core):
        self.check_available()
        return _VizAPI(core)

    def teardown(self) -> None:
        try:
            import matplotlib.pyplot as plt
            plt.close("all")
        except Exception:
            pass
