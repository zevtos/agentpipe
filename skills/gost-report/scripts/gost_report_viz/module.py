"""VizModule — namespace `r.plot`. Подключается лениво при первом обращении.

Эргономика: каждый хелпер либо встраивает рисунок (если передан caption) и
возвращает его номер (int), либо возвращает Figure-объект для ручного
r.figure(chart, caption). Один call-site для графика, диаграммы и готового PNG.
"""
from __future__ import annotations

from typing import Optional, Sequence

from core_api import ActionableImportError
from .charts import (
    BarChart, GroupedBarChart, Histogram, LineChart, ScatterChart,
    _as_series, _labels_for,
)


class _VizAPI:
    def __init__(self, core):
        self._core = core

    # --- общий хвост: встроить или вернуть Figure ---
    def _finish(self, chart, caption: Optional[str], width_cm: Optional[float]):
        chart._out_dir = self._core.tmp_dir
        if caption is None:
            return chart
        return self._core.embed_figure(chart, caption, width_cm=width_cm)

    def line(self, x: Sequence, y, *, labels=None, xlabel: str = "",
             ylabel: str = "", caption: Optional[str] = None,
             width_cm: Optional[float] = None):
        series = _as_series(y)
        chart = LineChart(xlabel=xlabel, ylabel=ylabel)
        chart.x = list(x)
        chart.series = series
        chart.labels = _labels_for(labels, len(series))
        return self._finish(chart, caption, width_cm)

    def scatter(self, x: Sequence, y, *, labels=None, xlabel: str = "",
                ylabel: str = "", caption: Optional[str] = None,
                width_cm: Optional[float] = None):
        series = _as_series(y)
        chart = ScatterChart(xlabel=xlabel, ylabel=ylabel)
        chart.x = list(x)
        chart.series = series
        chart.labels = _labels_for(labels, len(series))
        return self._finish(chart, caption, width_cm)

    def bar(self, categories: Sequence[str], values: Sequence, *,
            xlabel: str = "", ylabel: str = "", caption: Optional[str] = None,
            width_cm: Optional[float] = None):
        chart = BarChart(xlabel=xlabel, ylabel=ylabel)
        chart.categories = [str(c) for c in categories]
        chart.values = list(values)
        return self._finish(chart, caption, width_cm)

    def grouped_bar(self, categories: Sequence[str], series, *, labels=None,
                    xlabel: str = "", ylabel: str = "",
                    caption: Optional[str] = None,
                    width_cm: Optional[float] = None):
        series = _as_series(series)
        chart = GroupedBarChart(xlabel=xlabel, ylabel=ylabel)
        chart.categories = [str(c) for c in categories]
        chart.series = series
        chart.labels = _labels_for(labels, len(series))
        return self._finish(chart, caption, width_cm)

    def histogram(self, data: Sequence, *, bins: int = 20, xlabel: str = "",
                  ylabel: str = "Частота", caption: Optional[str] = None,
                  width_cm: Optional[float] = None):
        chart = Histogram(xlabel=xlabel, ylabel=ylabel)
        chart.data = list(data)
        chart.bins = bins
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
