"""registry — discovery подключаемых модулей gost-report.

Два канала, один контракт (`core_api.ReportModule`):

  (A) Built-in vendored — модули, лежащие папками внутри scripts/. Перечень
      явный (не auto-scan): детерминизм + контроль над тем, что доступно.
      Работает в папке-скилле БЕЗ pip, и на claude-, и на codex-таргете.

  (B) Pip entry points (`gost_report.modules`) — сторонние пакеты
      `gost-report-<module>`, подхватываются ТОЛЬКО если реально установлены в
      активный Python. На codex/skills-only этот канал просто молчит.

Built-in приоритетнее: сторонний пакет не может перехватить namespace ядра.

Зависит только от stdlib. Импорт модуля-класса лёгкий (тянет лишь core_api);
тяжёлые пакеты (matplotlib и т.п.) грузятся внутри модуля при первом использовании.
"""
from __future__ import annotations

import importlib
from typing import Dict


# namespace → "module_path:ClassName"
_BUILTIN: Dict[str, str] = {
    "plot": "gost_report_viz.module:VizModule",
    "diagram": "gost_report_diagrams.module:DiagramModule",
}

# Модули, которые присоединяются сразу при создании Report (их deps в default-тире).
# Пусто: viz/diagrams тянут тяжёлые deps → только lazy on first use.
_AUTO_ATTACH: frozenset = frozenset()


def _load(spec: str):
    mod_path, _, cls_name = spec.partition(":")
    module = importlib.import_module(mod_path)
    return getattr(module, cls_name)()


def discover() -> Dict[str, object]:
    """Вернуть {namespace: module_instance}. Любая ошибка импорта одного модуля
    не роняет остальные — отсутствующий vendored-модуль (частичная установка)
    просто пропускается."""
    found: Dict[str, object] = {}

    # (A) built-in vendored
    for ns, spec in _BUILTIN.items():
        try:
            found[ns] = _load(spec)
        except Exception:
            pass

    # (B) pip entry points (опционально)
    try:
        from importlib.metadata import entry_points

        eps = entry_points()
        # Python 3.10+: select(); старее — dict-like .get()
        group = (eps.select(group="gost_report.modules")
                 if hasattr(eps, "select")
                 else eps.get("gost_report.modules", []))
        for ep in group:
            try:
                inst = ep.load()()
                ns = getattr(inst, "namespace", None)
                if ns:
                    found.setdefault(ns, inst)  # built-in приоритетнее
            except Exception:
                pass
    except Exception:
        pass

    return found
