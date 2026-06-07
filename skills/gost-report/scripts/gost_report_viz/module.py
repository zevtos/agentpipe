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

    # --- санитайз пользовательских строк графика ---
    # Текст графика (легенда, подписи осей, label опорных линий, текст
    # аннотаций) запекается в PNG и обходит и _sanitize_prose, и docx-валидатор.
    # Прогоняем его через self._core.sanitize ДО сборки/прокидывания графика,
    # чтобы канонической (санированной) строкой определялся и контент-хэш PNG.
    # НЕ трогаем: числовые данные, value_fmt (спека формата), caption (его
    # санирует embed_figure), цвета/стили/маркеры/ha/va.
    def _s(self, text):
        # None/"" проходят без изменений; sanitize() — no-op на чистом тексте.
        return self._core.sanitize(text) if text else text

    def _s_labels(self, labels):
        if labels is None:
            return None
        return [self._s(x) for x in labels]

    def _s_lines(self, items):
        # Санируем ТОЛЬКО видимое человеку поле `label` в dict'ах hline/vline,
        # до того как norm_lines свернёт их в контент-ключ. Скаляры (голые
        # числа) не имеют label → возвращаются нетронутыми.
        if not items:
            return items
        out = []
        for it in items:
            if isinstance(it, dict) and it.get("label"):
                it = {**it, "label": self._s(it["label"])}
            out.append(it)
        return out

    def _s_annos(self, items):
        if not items:
            return items
        out = []
        for it in items:
            if it.get("text") is not None:
                it = {**it, "text": self._s(str(it["text"]))}
            out.append(it)
        return out

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
        hlines = self._s_lines(hlines)
        vlines = self._s_lines(vlines)
        annotations = self._s_annos(annotations)
        series = _as_series(y)
        chart = LineChart(xlabel=self._s(xlabel), ylabel=self._s(ylabel))
        chart.x = list(x)
        chart.series = series
        chart.labels = _labels_for(self._s_labels(labels), len(series))
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
        hlines = self._s_lines(hlines)
        vlines = self._s_lines(vlines)
        annotations = self._s_annos(annotations)
        series = _as_series(y)
        chart = ScatterChart(xlabel=self._s(xlabel), ylabel=self._s(ylabel))
        chart.x = list(x)
        chart.series = series
        chart.labels = _labels_for(self._s_labels(labels), len(series))
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
        hlines = self._s_lines(hlines)
        vlines = self._s_lines(vlines)
        annotations = self._s_annos(annotations)
        chart = BarChart(xlabel=self._s(xlabel), ylabel=self._s(ylabel))
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
        hlines = self._s_lines(hlines)
        vlines = self._s_lines(vlines)
        annotations = self._s_annos(annotations)
        series = _as_series(series)
        chart = GroupedBarChart(xlabel=self._s(xlabel), ylabel=self._s(ylabel))
        chart.categories = [str(c) for c in categories]
        chart.series = series
        chart.labels = _labels_for(self._s_labels(labels), len(series))
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
        hlines = self._s_lines(hlines)
        vlines = self._s_lines(vlines)
        annotations = self._s_annos(annotations)
        series = _as_series(series)
        chart = StackedBarChart(xlabel=self._s(xlabel), ylabel=self._s(ylabel))
        chart.categories = [str(c) for c in categories]
        chart.series = series
        chart.labels = _labels_for(self._s_labels(labels), len(series))
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
        hlines = self._s_lines(hlines)
        vlines = self._s_lines(vlines)
        annotations = self._s_annos(annotations)
        series = _as_series(y)
        chart = AreaChart(xlabel=self._s(xlabel), ylabel=self._s(ylabel))
        chart.x = list(x)
        chart.series = series
        chart.labels = _labels_for(self._s_labels(labels), len(series))
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
        hlines = self._s_lines(hlines)
        vlines = self._s_lines(vlines)
        annotations = self._s_annos(annotations)
        chart = Histogram(xlabel=self._s(xlabel), ylabel=self._s(ylabel))
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
