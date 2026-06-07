"""Единый ГОСТ-пресет matplotlib + палитры с grayscale-страховкой.

Решения (research/19):
- Шрифт: Liberation Serif → Times New Roman → PT Serif → DejaVu Serif (fallback-
  цепочка; matplotlib берёт первый доступный). mathtext.fontset='stix' — serif-
  совместимая математика, идёт в комплекте matplotlib.
- Палитра по умолчанию — Okabe-Ito (CVD-safe, разная светлота → читается в Ч/Б).
- Второй канал (linestyle+marker для линий, hatch для столбцов) дублирует hue,
  чтобы график различался и в чёрно-белой печати.
- savefig: metadata Software=None для байт-стабильности PNG.
"""
from __future__ import annotations

# Okabe & Ito, Color Universal Design (jfly.uni-koeln.de/color) — 8 цветов.
OKABE_ITO = [
    "#E69F00", "#56B4E9", "#009E73", "#F0E442",
    "#0072B2", "#D55E00", "#CC79A7", "#000000",
]

# Paul Tol high-contrast — лучший greyscale для ≤3 серий (SRON/EPS/TN/09-002).
TOL_HIGH_CONTRAST = ["#004488", "#DDAA33", "#BB5566"]

FONT_SERIF_CHAIN = [
    "Liberation Serif", "Times New Roman", "PT Serif", "DejaVu Serif",
]

# Второй канал — дублирует цвет формой, для Ч/Б.
LINESTYLES = ["-", "--", "-.", ":"]
MARKERS = ["o", "s", "^", "D", "v", "P", "X", "*"]
HATCHES = ["", "//", "\\\\", "xx", "..", "++", "oo", "**"]

# Опорные линии (hlines/vlines): нейтральный серый пунктир, читается как
# вспомогательная линия и в Ч/Б. Дефолты переопределяются per-line dict'ом.
REFLINE_COLOR = "#444444"
REFLINE_LINESTYLE = "--"
REFLINE_LINEWIDTH = 1.0

_STYLE_APPLIED = False


def apply_gost_style(force: bool = False) -> None:
    """Накатывает rcParams-пресет на глобальный matplotlib. Идемпотентно
    (по флагу), но безопасно вызывать перед каждым рендером."""
    global _STYLE_APPLIED
    if _STYLE_APPLIED and not force:
        return
    import matplotlib
    from cycler import cycler

    matplotlib.rcParams.update({
        "font.family": "serif",
        "font.serif": FONT_SERIF_CHAIN,
        "mathtext.fontset": "stix",
        "font.size": 11,
        "axes.titlesize": 12,
        "axes.labelsize": 11,
        "xtick.labelsize": 10,
        "ytick.labelsize": 10,
        "legend.fontsize": 10,
        "axes.linewidth": 0.8,
        "axes.grid": True,
        "axes.axisbelow": True,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "grid.linewidth": 0.5,
        "grid.alpha": 0.4,
        "grid.color": "#b0b0b0",
        "lines.linewidth": 1.6,
        "lines.markersize": 5,
        "legend.frameon": True,
        "legend.framealpha": 0.9,
        "legend.edgecolor": "#888888",
        "figure.dpi": 300,
        "savefig.dpi": 300,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.05,
        "svg.hashsalt": "gost-report",
        "axes.prop_cycle": cycler(color=OKABE_ITO),
    })
    _STYLE_APPLIED = True


def second_channel(i: int) -> dict:
    """linestyle+marker для i-й серии (дублирует цвет формой)."""
    return {
        "color": OKABE_ITO[i % len(OKABE_ITO)],
        "linestyle": LINESTYLES[i % len(LINESTYLES)],
        "marker": MARKERS[i % len(MARKERS)],
    }


def series_color(i: int) -> str:
    return OKABE_ITO[i % len(OKABE_ITO)]


def series_hatch(i: int) -> str:
    return HATCHES[i % len(HATCHES)]
