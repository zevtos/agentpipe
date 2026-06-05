#!/usr/bin/env python3
"""gr — лаунчер gost-report. Запускает build-скрипт в venv скилла (через
ensure_env.py — бутстрап + изоляция зависимостей).

    gr путь/к/build.py        запустить конкретный скрипт
    gr                        найти .claude/gost-report/build.py вверх от cwd
                              (дефолтный путь, куда Claude кладёт билды) и запустить

Кросс-платформенно (один и тот же код под bash- и .cmd-шимом). Stdlib-only."""
from __future__ import annotations

import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENSURE = SCRIPTS / "ensure_env.py"
DEFAULT_REL = Path(".claude") / "gost-report" / "build.py"


def _find_default_build() -> "Path | None":
    """Обход вверх от cwd до первого .claude/gost-report/build.py."""
    cur = Path.cwd().resolve()
    for d in (cur, *cur.parents):
        cand = d / DEFAULT_REL
        if cand.is_file():
            return cand
    return None


def main(argv: list) -> int:
    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__ + "\n")
        return 0

    if argv and not argv[0].startswith("-"):
        build = Path(argv[0]).expanduser()
        rest = argv[1:]
        if not build.is_file():
            sys.stderr.write(f"gr: файл не найден: {build}\n")
            return 2
    else:
        build = _find_default_build()
        rest = argv
        if build is None:
            sys.stderr.write(
                "gr: build-скрипт не найден.\n"
                "  Передай путь явно:      gr путь/к/build.py\n"
                f"  Или создай дефолтный:   ./{DEFAULT_REL}\n"
                "  (Claude кладёт билды именно туда.)\n")
            return 2
        sys.stderr.write(f"gr: запускаю {build}\n")

    # ensure_env.py сам поднимет venv и выполнит build его питоном.
    cmd = [sys.executable, str(ENSURE), str(build), *rest]
    if os.name == "nt":
        import subprocess
        return subprocess.run(cmd).returncode
    os.execv(sys.executable, cmd)
    return 0  # unreachable (POSIX)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
