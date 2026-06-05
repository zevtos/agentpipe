"""Бэкенды рендера mermaid → PNG. Каскад, первый доступный выигрывает:

  1. mmdc   — npm @mermaid-js/mermaid-cli (Node+Chromium). Эталонная точность,
              оффлайн. Берётся с PATH (pip-ом не ставится).
  2. merm   — pure-python (pip, тир [diagrams]). Без Node/браузера, оффлайн.
              Реимплементация mermaid → возможны расхождения на сложном синтаксисе.
  3. mermaid.ink — HTTP-сервис. Только при GOST_REPORT_DIAGRAMS_ONLINE=1
              (сеть + диаграмма уходит на сторонний сервер).

Все дают PNG. Если бэкенд отдаёт SVG — конвертация через cairosvg (если есть).
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple


class DiagramError(RuntimeError):
    pass


def _which(name: str) -> Optional[str]:
    """Ищет исполняемый файл на PATH и рядом с текущим питоном (venv/bin)."""
    found = shutil.which(name)
    if found:
        return found
    for ext in ("", ".exe", ".cmd"):
        cand = Path(sys.executable).parent / (name + ext)
        if cand.exists():
            return str(cand)
    return None


def available_backends() -> List[str]:
    out = []
    if _which("mmdc"):
        out.append("mmdc")
    if _which("merm"):
        out.append("merm")
    if os.environ.get("GOST_REPORT_DIAGRAMS_ONLINE") == "1":
        out.append("mermaid.ink")
    return out


def _svg_to_png(svg_path: Path, png_path: Path, *, dpi: int, scale: int) -> None:
    """SVG → PNG каскадом растеризаторов (первый доступный). cairosvg тянет
    системный libcairo, поэтому НЕ ставится тиром — но используется, если есть.
    rsvg-convert / resvg — бинарные CLI без pip (brew install librsvg)."""
    # 1) cairosvg (pip + системный libcairo)
    try:
        import cairosvg
        cairosvg.svg2png(url=str(svg_path), write_to=str(png_path),
                         dpi=dpi, scale=scale)
        return
    except ImportError:
        pass
    except Exception as e:
        raise DiagramError(f"cairosvg failed: {e}") from e

    # 2) rsvg-convert (librsvg)
    rsvg = _which("rsvg-convert")
    if rsvg:
        proc = subprocess.run(
            [rsvg, "--zoom", str(scale), "-d", str(dpi), "-p", str(dpi),
             str(svg_path), "-o", str(png_path)],
            capture_output=True, text=True)
        if proc.returncode == 0 and png_path.exists():
            return
        raise DiagramError(f"rsvg-convert failed: {proc.stderr.strip()}")

    # 3) resvg
    resvg = _which("resvg")
    if resvg:
        proc = subprocess.run(
            [resvg, "--zoom", str(scale), str(svg_path), str(png_path)],
            capture_output=True, text=True)
        if proc.returncode == 0 and png_path.exists():
            return
        raise DiagramError(f"resvg failed: {proc.stderr.strip()}")

    raise DiagramError(
        "merm дал SVG, но нет растеризатора в PNG. Поставь любой:\n"
        "  • pip install cairosvg   (нужен системный libcairo)\n"
        "  • brew install librsvg   (даёт rsvg-convert)\n"
        "  • resvg                  (бинарь)\n"
        "Или используй точный бэкенд mmdc (PNG напрямую): "
        "npm i -g @mermaid-js/mermaid-cli"
    )


def _render_mmdc(src: str, out: Path, *, dpi: int, scale: int) -> Path:
    mmdc = _which("mmdc")
    mmd = out.with_suffix(".mmd")
    mmd.write_text(src, encoding="utf-8")
    cmd = [mmdc, "-i", str(mmd), "-o", str(out), "-b", "white", "-s", str(scale)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise DiagramError(f"mmdc failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return out


def _render_merm(src: str, out: Path, *, dpi: int, scale: int) -> Path:
    # merm нативный PNG требует cairosvg, поэтому всегда рендерим SVG и
    # растеризуем сами каскадом (rsvg-convert/cairosvg/resvg).
    merm = _which("merm")
    mmd = out.with_suffix(".mmd")
    mmd.write_text(src, encoding="utf-8")
    svg = out.with_suffix(".svg")
    # merm игнорирует %%{init}%%-themeVariables, но понимает --theme; neutral —
    # ближайший к ГОСТ (нейтральный, без ярких заливок).
    proc = subprocess.run([merm, str(mmd), "-f", "svg", "--theme", "neutral",
                           "-o", str(svg)], capture_output=True, text=True)
    if proc.returncode != 0 or not svg.exists():
        raise DiagramError(f"merm failed: {proc.stderr.strip() or proc.stdout.strip()}")
    _svg_to_png(svg, out, dpi=dpi, scale=scale)
    return out


def _render_ink(src: str, out: Path, *, dpi: int, scale: int) -> Path:
    import urllib.request
    enc = base64.urlsafe_b64encode(src.encode("utf-8")).decode("ascii")
    url = f"https://mermaid.ink/img/{enc}?type=png&bgColor=white"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = resp.read()
    except Exception as e:
        raise DiagramError(f"mermaid.ink request failed: {e}") from e
    out.write_bytes(data)
    return out


_BACKENDS: dict = {
    "mmdc": _render_mmdc,
    "merm": _render_merm,
    "mermaid.ink": _render_ink,
}


def render(src: str, out: Path, *, dpi: int = 300, scale: int = 3,
           prefer: Optional[str] = None) -> Tuple[Path, str]:
    """Рендерит src → out.png первым доступным бэкендом. Возвращает (путь, имя
    бэкенда). prefer форсирует конкретный бэкенд, если он доступен."""
    order = available_backends()
    if not order:
        raise DiagramError(
            "Нет ни одного бэкенда mermaid. Варианты:\n"
            "  • поставить pure-python бэкенд: "
            "GOST_REPORT_EXTRAS=diagrams python3 scripts/ensure_env.py\n"
            "  • поставить mmdc (точнее): npm i -g @mermaid-js/mermaid-cli\n"
            "  • разрешить онлайн-рендер: GOST_REPORT_DIAGRAMS_ONLINE=1 (сеть)"
        )
    if prefer and prefer in order:
        order = [prefer] + [b for b in order if b != prefer]
    last: Optional[Exception] = None
    for name in order:
        try:
            path = _BACKENDS[name](src, out, dpi=dpi, scale=scale)
            return path, name
        except DiagramError as e:
            last = e
    raise DiagramError(f"Все бэкенды mermaid упали; последний: {last}")
