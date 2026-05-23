"""
templates — Jinja2 environment + custom filters for synthesize_v2.

Templates themselves live at `skills/ultrasearch/templates/*.j2` (next to
the skill root, not under scripts/). This module owns environment setup
plus the few text-processing filters shared across templates.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from _common import skill_dir


_ENV = None


def _build_env():
    """Lazy-create the Jinja2 environment. Raises ImportError with a
    helpful message if jinja2 is not installed."""
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
    except ImportError as e:
        raise ImportError(
            "jinja2 is required for ultrasearch v2 template synthesis. "
            "Install with `pip install jinja2` (Stage 1 base dep)."
        ) from e
    tpl_dir = skill_dir() / "templates"
    env = Environment(
        loader=FileSystemLoader(str(tpl_dir)),
        autoescape=select_autoescape(default=False, default_for_string=False),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["truncate_chars"] = lambda s, n=200: (s or "")[:n].rstrip() + ("…" if s and len(s) > n else "")
    env.filters["ref_label"] = lambda i: f"[S{i+1}]" if isinstance(i, int) else f"[S{i}]"
    env.filters["compact"] = lambda s: " ".join((s or "").split())
    return env


def render(template_name: str, context: dict[str, Any]) -> str:
    """Render `template_name` from skill_dir()/templates/ with the given context."""
    global _ENV
    if _ENV is None:
        _ENV = _build_env()
    tpl = _ENV.get_template(template_name)
    return tpl.render(**context)


def template_path(name: str) -> Path:
    return skill_dir() / "templates" / name
