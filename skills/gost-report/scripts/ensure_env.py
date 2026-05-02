#!/usr/bin/env python3
"""
ensure_env.py — изолированное окружение для скилла gost-report.

Расположение скрипта: <skill_dir>/scripts/ensure_env.py
Расположение venv:    <skill_dir>/.venv/   (всегда, независимо от CWD)

Зачем это:
    Скилл нужен зависимостям (python-docx, latex2mathml), но ставить их
    глобально в системный Python — некрасиво и ломкое. Этот скрипт создаёт
    изолированное окружение прямо рядом со скиллом, по фиксированному пути,
    и поддерживает его в актуальном состоянии при апдейтах.

Поведение:
    1. Считает sha256 от scripts/requirements.txt.
    2. Если venv существует и его .installed_hash совпадает — мгновенный no-op.
    3. Иначе создаёт venv (порядок: uv → conda --prefix → python -m venv) и
       ставит/обновляет deps.
    4. Записывает свежий хэш в .venv/.installed_hash.

Использование:
    python3 ensure_env.py                  # bootstrap + вывести путь к venv-питону
    python3 ensure_env.py <script> [args]  # bootstrap + запустить script venv-питоном

Зависит только от stdlib — иначе курица-яйцо. Поддерживает Python 3.8+.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
VENV_DIR = SKILL_DIR / ".venv"
REQ_FILE = SCRIPTS_DIR / "requirements.txt"
HASH_FILE = VENV_DIR / ".installed_hash"
LOCK_FILE = SKILL_DIR / ".venv.lock"
PTH_NAME = "gost_report.pth"


def log(msg: str) -> None:
    """Прогресс на stderr — чтобы stdout оставался чистым (там путь к питону)."""
    print(f"[gost-report env] {msg}", file=sys.stderr, flush=True)


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def req_hash() -> str:
    return hashlib.sha256(REQ_FILE.read_bytes()).hexdigest()


def needs_update() -> bool:
    if not venv_python().exists():
        return True
    if not HASH_FILE.exists():
        return True
    return HASH_FILE.read_text().strip() != req_hash()


def create_venv() -> None:
    if have("uv"):
        log(f"Creating venv via uv at {VENV_DIR}")
        subprocess.run(["uv", "venv", str(VENV_DIR)], check=True)
        return
    if have("conda"):
        log(f"Creating venv via conda --prefix at {VENV_DIR} (this may take a minute)")
        subprocess.run(
            ["conda", "create", "--prefix", str(VENV_DIR),
             "python", "pip", "-y", "-q"],
            check=True,
        )
        return
    log(f"Creating venv via python -m venv at {VENV_DIR}")
    subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)


def site_packages_dir() -> Path | None:
    """Спрашиваем у самого venv-питона, где у него purelib. Не хардкодим
    `lib/pythonX.Y/site-packages` — Windows, conda и future-Python отличаются.
    Возвращаем None, если запрос упал (тогда вызывающий должен залогировать
    и продолжить — .pth не критичен, есть PYTHONPATH-страховка)."""
    py = venv_python()
    if not py.exists():
        return None
    try:
        out = subprocess.run(
            [str(py), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None
    if not out:
        return None
    return Path(out)


def write_pth() -> None:
    """Пишем <venv>/<purelib>/gost_report.pth с абсолютным путём к scripts/.
    Делает gost_report импортируемым любым процессом, использующим venv-питон,
    без правки sys.path в user-скрипте. Если упадёт — не критично, есть
    PYTHONPATH-страховка в main() для запусков через ensure_env.py."""
    sp = site_packages_dir()
    if sp is None:
        log("Could not resolve venv site-packages; skipping .pth write")
        return
    try:
        sp.mkdir(parents=True, exist_ok=True)
        pth = sp / PTH_NAME
        content = str(SCRIPTS_DIR.resolve()) + "\n"
        if pth.exists() and pth.read_text(encoding="utf-8") == content:
            return
        pth.write_text(content, encoding="utf-8")
        log(f"Wrote {pth.name} → {SCRIPTS_DIR}")
    except OSError as e:
        log(f"Failed to write {PTH_NAME}: {e}")


def pth_exists() -> bool:
    sp = site_packages_dir()
    if sp is None:
        return False
    return (sp / PTH_NAME).exists()


def install_deps() -> None:
    py = str(venv_python())
    log(f"Installing/updating dependencies from {REQ_FILE.name}")
    if have("uv"):
        subprocess.run(
            ["uv", "pip", "install", "--python", py,
             "-r", str(REQ_FILE), "--upgrade"],
            check=True,
        )
    else:
        subprocess.run(
            [py, "-m", "pip", "install",
             "-r", str(REQ_FILE), "--upgrade", "--quiet",
             "--disable-pip-version-check"],
            check=True,
        )


def _acquire_lock():
    """Best-effort кросс-платформенный flock на LOCK_FILE.

    Если две параллельные ensure_env.py стартуют одновременно — одна берёт
    блокировку и делает работу, вторая ждёт и потом видит, что всё уже готово.
    Если flock на платформе не работает — продолжаем без него (худшее, что
    случится: дублирующая pip-install, не катастрофа).
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        fh = open(LOCK_FILE, "w")
    except OSError:
        return None
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except Exception:
        pass
    return fh


def _release_lock(fh) -> None:
    if fh is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


def bootstrap() -> None:
    # Self-heal: даже если deps in sync, .pth мог быть удалён руками — переписываем.
    if not needs_update():
        if not pth_exists():
            lock = _acquire_lock()
            try:
                if not pth_exists():
                    write_pth()
            finally:
                _release_lock(lock)
        return
    lock = _acquire_lock()
    try:
        if not needs_update():
            if not pth_exists():
                write_pth()
            return
        VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
        if not venv_python().exists():
            create_venv()
        install_deps()
        write_pth()
        HASH_FILE.write_text(req_hash())
        log("Environment ready")
    finally:
        _release_lock(lock)


def main() -> int:
    if not REQ_FILE.exists():
        log(f"requirements.txt not found at {REQ_FILE}")
        return 1
    try:
        bootstrap()
    except subprocess.CalledProcessError as e:
        log(f"Bootstrap failed (exit {e.returncode}): {' '.join(map(str, e.cmd))}")
        return 1
    except OSError as e:
        log(f"Bootstrap failed: {e}")
        return 1

    args = sys.argv[1:]
    py = str(venv_python())
    if not args:
        print(py)
        return 0

    # Belt-and-suspenders: даже если .pth почему-то не сработал (старый venv,
    # пользователь убил файл, сторонний инструмент вычистил site-packages) —
    # PYTHONPATH гарантирует import gost_report для запусков через ensure_env.py.
    # Утечёт в подпроцессы user-скрипта; имя gost_report достаточно уникально,
    # чтобы это не было проблемой.
    scripts_dir = str(SCRIPTS_DIR.resolve())
    existing = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = (
        scripts_dir + os.pathsep + existing if existing else scripts_dir
    )

    if os.name == "nt":
        rc = subprocess.run([py, *args]).returncode
        return rc
    os.execv(py, [py, *args])
    return 0  # unreachable


if __name__ == "__main__":
    sys.exit(main())
