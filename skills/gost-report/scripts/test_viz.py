#!/usr/bin/env python3
"""Standalone test suite for gost_report_viz (no pytest — repo has no harness).

Mirrors the style of scripts/validate-*.py: assertion-based, runnable via
`python3 scripts/test_viz.py`, exits non-zero on any failure. Requires the
'viz' tier (numpy + matplotlib); build it with
`GOST_REPORT_EXTRAS=viz python3 scripts/ensure_env.py` or reuse any python
that imports numpy+matplotlib.

What it covers:
  1. DETERMINISM (the #1 regression risk): identical chart inputs -> identical
     PNG filename; and every annotation/axis param (hlines, vlines,
     value_labels, value_fmt, ylim, xlim, yscale, annotations, colors,
     linestyles, markers, horizontal, stacked, bins) flips the filename —
     proving the param is folded into _content_key(). norm-equivalence of
     hlines=[5] vs hlines=[{'value':5}] is also asserted.
  2. RENDER: every chart type with every new param renders without raising and
     writes a real, non-empty PNG to disk.
  3. BACKWARD COMPAT: the pre-existing _VizAPI call signatures
     (line/scatter/bar/grouped_bar/histogram with NO new kwargs) still work,
     both returning a Figure (caption=None) and embedding (caption=...).
  4. LEGEND: a labeled reference line shows up in the axes legend handles.

Exit codes:
    0  all tests passed
    1  at least one test failed (AssertionError or unexpected render error)
    2  environment missing (numpy/matplotlib not importable)
"""
from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _require_env() -> None:
    try:
        import numpy  # noqa: F401
        import matplotlib  # noqa: F401
    except ImportError as e:
        sys.stderr.write(
            f"SKIP-FATAL: viz tier not available ({e}). Build it with\n"
            "  GOST_REPORT_EXTRAS=viz python3 scripts/ensure_env.py\n"
            "or run with a python that imports numpy+matplotlib.\n"
        )
        raise SystemExit(2)


_require_env()

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from gost_report_viz.charts import (  # noqa: E402
    AreaChart, BarChart, GroupedBarChart, Histogram, LineChart, ScatterChart,
    StackedBarChart, norm_lines,
)
from gost_report_viz.module import _VizAPI, _check_yscale  # noqa: E402


# ----------------------------------------------------------------------------
# tiny test harness
# ----------------------------------------------------------------------------
_FAILURES: list[str] = []
_PASSED = 0


def check(name: str, fn) -> None:
    global _PASSED
    try:
        fn()
    except Exception:  # noqa: BLE001 — collect, don't abort the suite
        _FAILURES.append(name + "\n" + traceback.format_exc())
        sys.stderr.write(f"FAIL  {name}\n")
    else:
        _PASSED += 1
        print(f"ok    {name}")


def assert_eq(a, b, msg=""):
    assert a == b, f"{msg}: {a!r} != {b!r}"


def assert_ne(a, b, msg=""):
    assert a != b, f"{msg}: both == {a!r}"


# Shared temp output dir; charts hash on content, so dir does not affect name.
_TMP = Path(tempfile.mkdtemp(prefix="viz_test_"))


def _render(chart, out_dir: Path | None = None) -> Path:
    chart._out_dir = out_dir or _TMP
    return chart.render()


def _fname(chart, out_dir: Path | None = None) -> str:
    """Render and return just the filename (content-addressed)."""
    return _render(chart, out_dir).name


# ----------------------------------------------------------------------------
# 1. DETERMINISM — same inputs -> same filename across different dirs
# ----------------------------------------------------------------------------
def test_determinism_same_inputs_same_name():
    d1 = Path(tempfile.mkdtemp(prefix="viz_d1_"))
    d2 = Path(tempfile.mkdtemp(prefix="viz_d2_"))
    a = LineChart(xlabel="t", ylabel="U", x=[0, 1, 2], series=[[1.0, 2.0, 3.0]],
                  labels=[None])
    b = LineChart(xlabel="t", ylabel="U", x=[0, 1, 2], series=[[1.0, 2.0, 3.0]],
                  labels=[None])
    p1 = _render(a, d1)
    p2 = _render(b, d2)
    assert_eq(p1.name, p2.name, "same inputs must yield same filename")
    assert_eq(p1.read_bytes(), p2.read_bytes(),
              "same inputs must yield byte-identical PNG")


# Each entry: (label, baseline_factory, variant_factory).
# baseline and variant differ ONLY in the param under test; if the param is in
# _content_key(), filenames differ. If not, they collide (regression).
def _line(**kw):
    base = dict(xlabel="t", ylabel="U", x=[0, 1, 2, 3],
                series=[[1.0, 2.0, 1.5, 3.0]], labels=[None])
    base.update(kw)
    return LineChart(**base)


def _bar(**kw):
    base = dict(categories=["A", "B", "C"], values=[3.0, 5.0, 4.0])
    base.update(kw)
    return BarChart(**base)


def _grouped(**kw):
    base = dict(categories=["A", "B"], series=[[1.0, 2.0], [3.0, 4.0]],
                labels=["s1", "s2"])
    base.update(kw)
    return GroupedBarChart(**base)


def _scatter(**kw):
    base = dict(x=[0, 1, 2], series=[[1.0, 2.0, 3.0]], labels=[None])
    base.update(kw)
    return ScatterChart(**base)


def _hist(**kw):
    base = dict(data=[1.0, 2.0, 2.0, 3.0, 3.0, 3.0, 4.0])
    base.update(kw)
    return Histogram(**base)


def _stacked(**kw):
    base = dict(categories=["A", "B"], series=[[1.0, 2.0], [3.0, 4.0]],
                labels=["s1", "s2"])
    base.update(kw)
    return StackedBarChart(**base)


def _area(**kw):
    base = dict(x=[0, 1, 2], series=[[1.0, 2.0, 3.0]], labels=[None])
    base.update(kw)
    return AreaChart(**base)


# (name, baseline, variant) — variant must NOT collide with baseline.
_PARAM_VARIANTS = [
    ("hlines flips name",
     lambda: _line(),
     lambda: _line(hlines=norm_lines([{"value": 2.0, "label": "ref"}]))),
    ("vlines flips name",
     lambda: _line(),
     lambda: _line(vlines=norm_lines([{"value": 1.0, "label": "v"}]))),
    ("hline value flips name",
     lambda: _line(hlines=norm_lines([2.0])),
     lambda: _line(hlines=norm_lines([2.5]))),
    ("hline label flips name",
     lambda: _line(hlines=norm_lines([{"value": 2.0, "label": "a"}])),
     lambda: _line(hlines=norm_lines([{"value": 2.0, "label": "b"}]))),
    ("ylim flips name",
     lambda: _line(),
     lambda: _line(ylim=(0.0, 5.0))),
    ("xlim flips name",
     lambda: _line(),
     lambda: _line(xlim=(0.0, 3.0))),
    ("yscale flips name",
     lambda: _line(),
     lambda: _line(yscale="log")),
    ("annotations flips name",
     lambda: _line(),
     lambda: _line(annotations=[{"x": 1.0, "y": 2.0, "text": "peak", "dx": 6.0,
                                 "dy": 6.0, "arrow": False, "color": None,
                                 "ha": None, "va": None}])),
    ("line colors flips name",
     lambda: _line(),
     lambda: _line(colors=["#123456"])),
    ("line linestyles flips name",
     lambda: _line(),
     lambda: _line(linestyles=[":"])),
    ("line markers=False flips name",
     lambda: _line(),
     lambda: _line(markers=False)),
    ("line markers list flips name",
     lambda: _line(),
     lambda: _line(markers=["x"])),
    ("scatter colors flips name",
     lambda: _scatter(),
     lambda: _scatter(colors=["#abcdef"])),
    ("scatter markers flips name",
     lambda: _scatter(),
     lambda: _scatter(markers=["*"])),
    ("bar value_labels flips name",
     lambda: _bar(),
     lambda: _bar(value_labels=True)),
    ("bar value_fmt flips name",
     lambda: _bar(value_labels=True),
     lambda: _bar(value_labels=True, value_fmt="{:.1f}")),
    ("bar colors flips name",
     lambda: _bar(),
     lambda: _bar(colors=["#111111"])),
    ("bar horizontal flips name",
     lambda: _bar(),
     lambda: _bar(horizontal=True)),
    ("grouped value_labels flips name",
     lambda: _grouped(),
     lambda: _grouped(value_labels=True)),
    ("grouped value_fmt flips name",
     lambda: _grouped(value_labels=True),
     lambda: _grouped(value_labels=True, value_fmt="{:.2f}")),
    ("grouped colors flips name",
     lambda: _grouped(),
     lambda: _grouped(colors=["#222222"])),
    ("grouped horizontal flips name",
     lambda: _grouped(),
     lambda: _grouped(horizontal=True)),
    ("stacked value_labels flips name",
     lambda: _stacked(),
     lambda: _stacked(value_labels=True)),
    ("stacked colors flips name",
     lambda: _stacked(),
     lambda: _stacked(colors=["#333333"])),
    ("area stacked flips name",
     lambda: _area(),
     lambda: _area(stacked=True)),
    ("area colors flips name",
     lambda: _area(),
     lambda: _area(colors=["#444411"])),
    ("histogram bins flips name",
     lambda: _hist(bins=10),
     lambda: _hist(bins=20)),
    ("xlabel flips name",
     lambda: _line(),
     lambda: _line(xlabel="other")),
]


def make_param_test(baseline, variant, label):
    def _t():
        nb = _fname(baseline())
        nv = _fname(variant())
        assert_ne(nb, nv, f"param '{label}' not folded into _content_key()")
    return _t


def test_norm_equivalence_same_name():
    # hlines=[5] and hlines=[{'value':5}] must hash identically.
    a = _line(hlines=norm_lines([5]))
    b = _line(hlines=norm_lines([{"value": 5}]))
    assert_eq(_fname(a), _fname(b),
              "norm_lines([5]) and norm_lines([{'value':5}]) must collide")


def test_all_variants_distinct():
    # Stronger: render the whole zoo of distinct charts, assert no two share a
    # filename (no accidental cross-param collisions).
    charts = [
        _line(), _line(hlines=norm_lines([2.0])), _line(yscale="log"),
        _line(ylim=(0, 5)), _line(xlim=(0, 3)), _line(colors=["#123456"]),
        _line(markers=False), _bar(), _bar(value_labels=True), _bar(horizontal=True),
        _grouped(), _grouped(value_labels=True), _stacked(), _stacked(value_labels=True),
        _scatter(), _scatter(markers=["*"]), _area(), _area(stacked=True),
        _hist(bins=10), _hist(bins=25),
    ]
    names = [_fname(c) for c in charts]
    assert_eq(len(names), len(set(names)),
              f"collision among distinct charts: {sorted(names)}")


# ----------------------------------------------------------------------------
# 2. RENDER — every chart type + every new param writes a real PNG
# ----------------------------------------------------------------------------
def _assert_png(p: Path):
    assert p.exists(), f"render() returned non-existent path {p}"
    assert p.suffix == ".png", f"not a .png: {p}"
    data = p.read_bytes()
    assert len(data) > 100, f"PNG suspiciously small ({len(data)} bytes): {p}"
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"bad PNG magic in {p}"


_REFLINES = norm_lines([{"value": 2.0, "label": "ref"}])
_VLINES = norm_lines([{"value": 1.0, "label": "v"}])
_ANNOS = [{"x": 1.0, "y": 2.0, "text": "p", "dx": 6.0, "dy": 6.0,
           "arrow": True, "color": None, "ha": None, "va": None}]

_RENDER_CASES = [
    ("LineChart full", lambda: _line(
        hlines=_REFLINES, vlines=_VLINES, ylim=(0.5, 5), xlim=(0, 3),
        yscale="log", annotations=_ANNOS, colors=["#123456"],
        linestyles=[":"], markers=["x"])),
    ("LineChart markers=False", lambda: _line(markers=False)),
    ("LineChart multi-series", lambda: LineChart(
        x=[0, 1, 2], series=[[1.0, 2.0, 3.0], [3.0, 2.0, 1.0]],
        labels=["a", "b"])),
    ("ScatterChart full", lambda: _scatter(
        hlines=_REFLINES, vlines=_VLINES, ylim=(0, 5), annotations=_ANNOS,
        colors=["#abcdef"], markers=["*"])),
    ("BarChart full", lambda: _bar(
        value_labels=True, value_fmt="{:.1f}", colors=["#111111", "#222222"],
        hlines=_REFLINES)),
    ("BarChart horizontal+labels", lambda: _bar(
        horizontal=True, value_labels=True)),
    ("GroupedBarChart full", lambda: _grouped(
        value_labels=True, value_fmt="{:.0f}", colors=["#111", "#222"],
        hlines=_REFLINES)),
    ("GroupedBarChart horizontal", lambda: _grouped(horizontal=True,
                                                    value_labels=True)),
    ("StackedBarChart full", lambda: _stacked(
        value_labels=True, value_fmt="{:.1f}", colors=["#111", "#222"])),
    ("AreaChart overlap", lambda: _area(colors=["#444411"])),
    ("AreaChart stacked", lambda: AreaChart(
        x=[0, 1, 2], series=[[1.0, 2.0, 3.0], [1.0, 1.0, 1.0]],
        labels=["a", "b"], stacked=True)),
    ("Histogram full", lambda: _hist(bins=15, hlines=_REFLINES,
                                     annotations=_ANNOS)),
]


def make_render_test(factory):
    def _t():
        _assert_png(_render(factory()))
    return _t


# ----------------------------------------------------------------------------
# 3. BACKWARD COMPAT — pre-existing _VizAPI signatures still work
# ----------------------------------------------------------------------------
class _FakeCore:
    """Minimal core surface that _VizAPI._finish() depends on:
    tmp_dir (where charts render) and embed_figure (returns a fake fig number).
    """
    def __init__(self, tmp_dir: Path):
        self._tmp = tmp_dir
        self.embedded: list = []

    @property
    def tmp_dir(self) -> Path:
        return self._tmp

    def sanitize(self, text):
        # Mirror the real Report.sanitize/_sanitize_prose so the positive
        # tests below can assert that a dash-bearing chart label is normalized.
        if not text:
            return text
        import re
        text = re.sub(r"\s+[—–]\s+", ", ", text)
        text = re.sub(r"[—–]", "-", text)
        return text

    def embed_figure(self, chart, caption, *, width_cm=None):
        png = chart.render()  # exercise the real render path
        _assert_png(png)
        self.embedded.append((png, caption, width_cm))
        return len(self.embedded)  # фиктивный номер рисунка


def _api():
    return _VizAPI(_FakeCore(_TMP))


def test_bc_line_no_new_params():
    api = _api()
    fig = api.line([0, 1, 2], [1.0, 2.0, 3.0], xlabel="t", ylabel="U")
    _assert_png(fig.render())  # caption=None -> Figure returned


def test_bc_line_embed():
    api = _api()
    n = api.line([0, 1, 2], [1.0, 2.0, 3.0], xlabel="t", caption="Линия")
    assert_eq(n, 1, "embed_figure must return фиктивный номер 1")


def test_bc_scatter_no_new_params():
    api = _api()
    fig = api.scatter([0, 1, 2], [1.0, 2.0, 3.0], xlabel="x", ylabel="y")
    _assert_png(fig.render())


def test_bc_bar_no_new_params():
    api = _api()
    fig = api.bar(["A", "B", "C"], [3, 5, 4], ylabel="N")
    _assert_png(fig.render())


def test_bc_grouped_no_new_params():
    api = _api()
    fig = api.grouped_bar(["A", "B"], [[1, 2], [3, 4]], labels=["s1", "s2"])
    _assert_png(fig.render())


def test_bc_histogram_no_new_params():
    api = _api()
    fig = api.histogram([1, 2, 2, 3, 3, 3, 4], bins=5)
    _assert_png(fig.render())


def test_bc_multiseries_y_still_splits():
    # _as_series: a list-of-lists y must still produce a multi-series line.
    api = _api()
    fig = api.line([0, 1, 2], [[1, 2, 3], [3, 2, 1]], labels=["a", "b"])
    assert_eq(len(fig.series), 2, "list-of-lists y must yield 2 series")


def test_new_api_methods_exist():
    api = _api()
    for name in ("stacked_bar", "area"):
        assert hasattr(api, name), f"_VizAPI missing new method {name}"
    # smoke: they render through the embed path
    assert_eq(api.stacked_bar(["A", "B"], [[1, 2], [3, 4]], labels=["x", "y"],
                              caption="Stacked"), 1)
    assert_eq(api.area([0, 1, 2], [[1, 2, 3], [1, 1, 1]], labels=["x", "y"],
                       stacked=True, caption="Area"), 2)


def test_yscale_validation():
    api = _api()
    # valid passes
    api.line([0, 1], [1.0, 2.0], yscale="log")
    # invalid raises ValueError
    raised = False
    try:
        api.line([0, 1], [1.0, 2.0], yscale="logarithmic")
    except ValueError:
        raised = True
    assert raised, "bad yscale must raise ValueError"
    # the module-level helper too
    try:
        _check_yscale("nope")
    except ValueError:
        pass
    else:
        raise AssertionError("_check_yscale must reject unknown scale")


# ----------------------------------------------------------------------------
# 4. LEGEND — a labeled reference line appears in the legend handles
# ----------------------------------------------------------------------------
def test_hline_label_in_legend():
    plt.close("all")
    chart = _line(hlines=norm_lines([{"value": 2.0, "label": "порог"}]),
                  labels=["сигнал"])
    # mirror render()'s draw path on a throwaway axes to inspect legend labels
    fig, ax = plt.subplots()
    try:
        chart._draw(ax)
        chart._draw_reflines(ax)
        _, labels = ax.get_legend_handles_labels()
        assert "порог" in labels, f"hline label missing from legend: {labels}"
        assert "сигнал" in labels, f"series label missing from legend: {labels}"
    finally:
        plt.close(fig)


def test_vline_label_in_legend():
    plt.close("all")
    chart = _line(vlines=norm_lines([{"value": 1.0, "label": "событие"}]),
                  labels=[None])
    fig, ax = plt.subplots()
    try:
        chart._draw(ax)
        chart._draw_reflines(ax)
        _, labels = ax.get_legend_handles_labels()
        assert "событие" in labels, f"vline label missing from legend: {labels}"
    finally:
        plt.close(fig)


def test_unlabeled_refline_not_in_legend():
    plt.close("all")
    chart = _line(hlines=norm_lines([2.0]), labels=[None])  # no label
    fig, ax = plt.subplots()
    try:
        chart._draw(ax)
        chart._draw_reflines(ax)
        _, labels = ax.get_legend_handles_labels()
        assert not any(labels), f"unlabeled refline leaked into legend: {labels}"
    finally:
        plt.close(fig)


# ----------------------------------------------------------------------------
# 5. NUMPY-EQUIVALENCE — numpy x/data must hash the same as a Python list,
#    so a chart fed a numpy array reuses the content-addressed PNG (no dup render)
# ----------------------------------------------------------------------------
def test_numpy_x_equals_list_x():
    import numpy as np
    api = _api()
    pairs = [
        api.line([0, 1, 2], [1.0, 2.0, 3.0]),
        api.line(np.array([0, 1, 2]), [1.0, 2.0, 3.0]),
    ]
    assert_eq(pairs[0]._content_key(), pairs[1]._content_key(),
              "numpy x must hash identically to list x (line)")
    h = [api.histogram([1, 2, 3]), api.histogram(np.array([1, 2, 3]))]
    assert_eq(h[0]._content_key(), h[1]._content_key(),
              "numpy data must hash identically to list data (histogram)")
    a = [api.area([0, 1, 2], [[1, 2, 3]]),
         api.area(np.array([0, 1, 2]), [[1, 2, 3]])]
    assert_eq(a[0]._content_key(), a[1]._content_key(),
              "numpy x must hash identically to list x (area)")


# ----------------------------------------------------------------------------
# 6. SANITIZE — user-facing chart strings are normalized before they bake into
#    the PNG (GAP 1). A dash-bearing label/xlabel/annotation must come out
#    sanitized, and the resulting content key must differ from the raw variant.
# ----------------------------------------------------------------------------
def test_labels_sanitized():
    api = _api()
    fig = api.line([0, 1, 2], [1.0, 2.0, 3.0],
                   labels=["ток — измеренный"], xlabel="t — время",
                   ylabel="U — напряжение")
    assert_eq(fig.labels[0], "ток, измеренный",
              "legend label must be sanitized")
    assert_eq(fig.xlabel, "t, время", "xlabel must be sanitized")
    assert_eq(fig.ylabel, "U, напряжение", "ylabel must be sanitized")


def test_hline_label_sanitized():
    api = _api()
    fig = api.line([0, 1, 2], [1.0, 2.0, 3.0],
                   hlines=[{"value": 2.0, "label": "порог — верхний"}],
                   vlines=[{"value": 1.0, "label": "старт – t0"}])
    assert_eq(fig.hlines[0]["label"], "порог, верхний",
              "hline label must be sanitized")
    assert_eq(fig.vlines[0]["label"], "старт, t0",
              "vline label must be sanitized")


def test_annotation_text_sanitized():
    api = _api()
    fig = api.line([0, 1, 2], [1.0, 2.0, 3.0],
                   annotations=[{"x": 1.0, "y": 2.0, "text": "пик — максимум"}])
    assert_eq(fig.annotations[0]["text"], "пик, максимум",
              "annotation text must be sanitized")


def test_sanitized_label_changes_content_key():
    api = _api()
    clean = api.line([0, 1, 2], [1.0, 2.0, 3.0], labels=["ток, измеренный"])
    dashed = api.line([0, 1, 2], [1.0, 2.0, 3.0], labels=["ток — измеренный"])
    # The dashed label is canonicalized to the same string as the clean one,
    # so the content key (and PNG bytes) must match — sanitize is the canon.
    assert_eq(clean._content_key(), dashed._content_key(),
              "dashed label must canonicalize to clean label's content key")


def test_scalar_hlines_untouched():
    api = _api()
    # bare-number hlines have no label -> pass through unchanged, render fine
    fig = api.line([0, 1, 2], [1.0, 2.0, 3.0], hlines=[2.0], vlines=[1.0])
    _assert_png(fig.render())
    assert_eq(fig.hlines[0]["value"], 2.0, "scalar hline value preserved")
    assert_eq(fig.hlines[0]["label"], None, "scalar hline has no label")


# ----------------------------------------------------------------------------
# runner
# ----------------------------------------------------------------------------
def main() -> int:
    # determinism
    check("determinism: same inputs -> same name + bytes",
          test_determinism_same_inputs_same_name)
    check("determinism: norm_lines([5]) == norm_lines([{'value':5}])",
          test_norm_equivalence_same_name)
    for label, baseline, variant in _PARAM_VARIANTS:
        check(f"determinism: {label}", make_param_test(baseline, variant, label))
    check("determinism: 20 distinct charts -> 20 distinct names",
          test_all_variants_distinct)

    # render
    for label, factory in _RENDER_CASES:
        check(f"render: {label} writes PNG", make_render_test(factory))

    # backward compat
    check("bc: line (no new params)", test_bc_line_no_new_params)
    check("bc: line embed via fake core", test_bc_line_embed)
    check("bc: scatter (no new params)", test_bc_scatter_no_new_params)
    check("bc: bar (no new params)", test_bc_bar_no_new_params)
    check("bc: grouped_bar (no new params)", test_bc_grouped_no_new_params)
    check("bc: histogram (no new params)", test_bc_histogram_no_new_params)
    check("bc: list-of-lists y -> multi-series", test_bc_multiseries_y_still_splits)
    check("bc: numpy x/data hashes same as list (no dup render)",
          test_numpy_x_equals_list_x)
    check("api: new stacked_bar/area methods render", test_new_api_methods_exist)
    check("api: yscale allowlist validation", test_yscale_validation)

    # legend
    check("legend: labeled hline in legend handles", test_hline_label_in_legend)
    check("legend: labeled vline in legend handles", test_vline_label_in_legend)
    check("legend: unlabeled refline absent from legend",
          test_unlabeled_refline_not_in_legend)

    # sanitize (GAP 1)
    check("sanitize: legend/xlabel/ylabel normalized", test_labels_sanitized)
    check("sanitize: hline/vline label normalized", test_hline_label_sanitized)
    check("sanitize: annotation text normalized", test_annotation_text_sanitized)
    check("sanitize: dashed label canonicalizes to clean content key",
          test_sanitized_label_changes_content_key)
    check("sanitize: scalar hlines pass through untouched",
          test_scalar_hlines_untouched)

    total = _PASSED + len(_FAILURES)
    if _FAILURES:
        sys.stderr.write("\n" + "=" * 70 + "\n")
        for f in _FAILURES:
            sys.stderr.write(f + "\n")
        sys.stderr.write(
            f"\n{len(_FAILURES)}/{total} test(s) FAILED.\n")
        return 1
    print(f"\nOK  all {total} viz tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
