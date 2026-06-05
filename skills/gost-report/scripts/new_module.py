#!/usr/bin/env python3
"""Штамповка скелета подключаемого модуля gost-report.

    python3 scripts/new_module.py <namespace> [--visual] [--extra NAME]

Создаёт scripts/gost_report_<namespace>/ с module.py (+ figure.py для --visual),
печатает следующие шаги: строку в registry._BUILTIN и requirements.d/<extra>.txt.

См. research/19 §8 (кукбук авторинга) и core_api.py (контракты).
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent


def _module_py(ns: str, cls: str, extra: str, visual: bool) -> str:
    if visual:
        api_method = (
            "    def example(self, caption=None):\n"
            '        """Встраивает визуал (если есть caption) → номер рисунка,\n'
            '        иначе возвращает Figure для r.figure(fig, caption)."""\n'
            "        fig = _Fig(out_dir=self._core.tmp_dir)\n"
            "        if caption is None:\n"
            "            return fig\n"
            "        return self._core.embed_figure(fig, caption)\n"
        )
    else:
        api_method = (
            '    def example(self, text, caption=""):\n'
            '        """Пример: таблица из одной ячейки через core-сервис."""\n'
            "        return self._core.embed_table("
            "[[self._core.sanitize(text)]], caption)\n"
        )
    header_import = "from .figure import _Fig\n" if visual else ""
    requires = '"%s"' % extra if visual else "None"
    check_body = (
        "        # TODO: проверь реальные зависимости тира. Пример:\n"
        "        #   try:\n"
        "        #       import some_pkg  # noqa\n"
        "        #   except ImportError as e:\n"
        "        #       raise ActionableImportError.for_extra(\n"
        '        #           self.title, self.requires_extra, "some_pkg") from e\n'
        "        pass\n"
    )
    return (
        '"""%sModule — namespace r.%s. См. research/19 и core_api.py."""\n' % (cls, ns)
        + "from __future__ import annotations\n\n"
        + "from core_api import ActionableImportError  # noqa: F401\n"
        + header_import
        + "\n\n"
        + "class _%sAPI:\n" % cls
        + "    def __init__(self, core):\n"
        + "        self._core = core\n\n"
        + api_method
        + "\n\n"
        + "class %sModule:\n" % cls
        + '    namespace = "%s"\n' % ns
        + '    title = "TODO: человекочитаемое имя модуля"\n'
        + "    requires_extra = %s\n\n" % requires
        + "    def check_available(self):\n"
        + check_body
        + "\n"
        + "    def attach(self, core):\n"
        + "        self.check_available()\n"
        + "        return _%sAPI(core)\n\n" % cls
        + "    def teardown(self):\n"
        + "        pass\n"
    )


_FIGURE_PY = textwrap.dedent('''\
    """Figure-объект: render() детерминированно пишет PNG, возвращает путь.
    Нумерацию/подпись делает ядро (embed_figure), не этот класс."""
    from __future__ import annotations

    import hashlib
    import tempfile
    from pathlib import Path


    class _Fig:
        natural_width_cm = 16.0

        def __init__(self, *, out_dir=None):
            self._out_dir = out_dir

        def render(self, *, dpi=300, max_width_cm=16.0):
            # TODO: построить PNG. Для matplotlib:
            #   fig.savefig(out, dpi=dpi, metadata={"Software": None})
            key = hashlib.sha256(b"TODO-content").hexdigest()[:16]
            out_dir = self._out_dir or Path(tempfile.gettempdir())
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / (key + ".png")
            raise NotImplementedError("реализуй render() → PNG")
''')


def main(argv: list) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    ns = args[0].strip().lower()
    if not ns.isidentifier():
        print("namespace '%s' должен быть валидным идентификатором" % ns)
        return 1
    visual = "--visual" in argv
    extra = ns
    if "--extra" in argv:
        i = argv.index("--extra")
        if i + 1 < len(argv):
            extra = argv[i + 1]

    root = SCRIPTS_DIR / ("gost_report_%s" % ns)
    if root.exists():
        print("уже существует: %s" % root)
        return 1
    root.mkdir()
    cls = ns.capitalize()

    (root / "__init__.py").write_text(
        '"""gost_report_%s — подключаемый модуль (namespace r.%s)."""\n' % (ns, ns),
        encoding="utf-8")
    (root / "module.py").write_text(_module_py(ns, cls, extra, visual),
                                    encoding="utf-8")
    if visual:
        (root / "figure.py").write_text(_FIGURE_PY, encoding="utf-8")

    print("✓ Создан %s" % root)
    print("Дальше:")
    print('  1. registry.py → _BUILTIN: "%s": "gost_report_%s.module:%sModule"'
          % (ns, ns, cls))
    if visual:
        print("  2. requirements.d/%s.txt — пины зависимостей тира" % extra)
        print("  3. GOST_REPORT_EXTRAS=%s python3 scripts/ensure_env.py" % extra)
    print("  4. Реализуй методы в gost_report_%s/module.py%s"
          % (ns, " и figure.py" if visual else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
