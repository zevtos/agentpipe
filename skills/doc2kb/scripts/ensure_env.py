#!/usr/bin/env python3
"""
ensure_env.py — изолированное окружение для скилла doc2kb.

Расположение скрипта: <skill_dir>/scripts/ensure_env.py
Расположение venv:    <skill_dir>/.venv/   (всегда, независимо от CWD)

Зачем это:
    Скилл нужен зависимостям (pymupdf4llm, mammoth, python-pptx, ...), но
    ставить их глобально в системный Python — некрасиво и ломкое. Этот скрипт
    создаёт изолированное окружение прямо рядом со скиллом, по фиксированному
    пути, и поддерживает его в актуальном состоянии при апдейтах.

Поведение:
    1. Считает sha256 от scripts/requirements.txt.
    2. Если venv существует и его .installed_hash совпадает — мгновенный no-op.
    3. Иначе создаёт venv (порядок: uv → conda --prefix → python -m venv) и
       ставит/обновляет deps.
    4. Записывает свежий хэш в .venv/.installed_hash.

Использование:
    python3 ensure_env.py                          # lightweight bootstrap + путь к venv-питону
    python3 ensure_env.py <script> [args]          # lightweight bootstrap + запустить script
    python3 ensure_env.py --tier mineru            # additive: ставит mineru tier поверх lightweight
    python3 ensure_env.py --tier mineru <script>   # ensure mineru tier перед запуском script

Тиры (opt-in):
    lightweight (default) — requirements.txt, всегда поставлен.
    mineru                — requirements-mineru.txt, отдельный хэш-файл
                            (.installed_hash_mineru), additive, ставится только
                            при явном --tier mineru. ~3 GB + MLX wheels на Mac.

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
PTH_NAME = "doc2kb.pth"

# Opt-in tiers layered on top of the lightweight base. Adding a tier never
# touches the lightweight install — they live in the same venv but get their
# own hash file so re-running `--tier mineru` is idempotent and switching
# back to default invocation doesn't trigger a reinstall.
TIERS: dict[str, str] = {
    "mineru": "requirements-mineru.txt",
}


def log(msg: str) -> None:
    """Прогресс на stderr — чтобы stdout оставался чистым (там путь к питону)."""
    print(f"[doc2kb env] {msg}", file=sys.stderr, flush=True)


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


def tier_req_file(tier: str) -> Path:
    """Return the requirements file for the named opt-in tier. Raises
    ValueError for unknown tier names so the user gets an immediate error
    instead of a silent no-op install."""
    if tier not in TIERS:
        raise ValueError(
            f"unknown tier {tier!r}; available: {sorted(TIERS)}"
        )
    return SCRIPTS_DIR / TIERS[tier]


def tier_hash_file(tier: str) -> Path:
    """Per-tier hash file. Lives next to the lightweight HASH_FILE so we can
    check `--tier mineru` idempotency without re-touching the base install."""
    return VENV_DIR / f".installed_hash_{tier}"


def tier_needs_update(tier: str) -> bool:
    """True iff the tier's requirements file changed since last install (or
    the tier has never been installed in this venv)."""
    req = tier_req_file(tier)
    hash_file = tier_hash_file(tier)
    if not hash_file.exists():
        return True
    current = hashlib.sha256(req.read_bytes()).hexdigest()
    return hash_file.read_text().strip() != current


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
    """Пишем <venv>/<purelib>/doc2kb.pth с абсолютным путём к scripts/.
    Делает scripts/ импортируемым любым процессом, использующим venv-питон,
    без правки sys.path в user-скрипте."""
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


def install_deps(req_file: Path | None = None) -> None:
    """Install/upgrade everything in `req_file` into the venv.

    Defaults to the lightweight `requirements.txt`. Pass a tier file
    (`requirements-mineru.txt`, ...) to layer additional packages on top
    of the base install without touching the lightweight hash.
    """
    if req_file is None:
        req_file = REQ_FILE
    py = str(venv_python())
    log(f"Installing/updating dependencies from {req_file.name}")
    if have("uv"):
        subprocess.run(
            ["uv", "pip", "install", "--python", py,
             "-r", str(req_file), "--upgrade"],
            check=True,
        )
    else:
        subprocess.run(
            [py, "-m", "pip", "install",
             "-r", str(req_file), "--upgrade", "--quiet",
             "--disable-pip-version-check"],
            check=True,
        )


def _acquire_lock():
    """Best-effort кросс-платформенный flock на LOCK_FILE.

    Если две параллельные ensure_env.py стартуют одновременно — одна берёт
    блокировку и делает работу, вторая ждёт и потом видит, что всё уже готово.
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


def bootstrap_tier(tier: str) -> None:
    """Install the named opt-in tier on top of the lightweight base. No-op
    when the tier's hash matches the on-disk requirements file."""
    req = tier_req_file(tier)
    if not req.exists():
        raise FileNotFoundError(
            f"tier {tier!r} requirements file missing: {req}"
        )
    bootstrap()  # lightweight base must exist first
    if not tier_needs_update(tier):
        return
    lock = _acquire_lock()
    try:
        if not tier_needs_update(tier):
            return
        log(f"Installing opt-in tier {tier!r} from {req.name} (this may "
            "take several minutes — heavy ML deps)")
        install_deps(req)
        current = hashlib.sha256(req.read_bytes()).hexdigest()
        tier_hash_file(tier).write_text(current)
        log(f"Tier {tier!r} ready")
    finally:
        _release_lock(lock)


def _parse_tier_flag(argv: list[str]) -> tuple[str | None, list[str]]:
    """Strip an optional `--tier <name>` (or `--tier=<name>`) from the head
    of argv before the script name. Returns (tier, remaining_args). Kept
    minimal — no argparse — so ensure_env.py keeps a stdlib-only footprint
    and can run before its own venv exists.
    """
    if not argv:
        return None, argv
    first = argv[0]
    if first == "--tier":
        if len(argv) < 2:
            raise ValueError("--tier requires a value (e.g. --tier mineru)")
        return argv[1], argv[2:]
    if first.startswith("--tier="):
        value = first.split("=", 1)[1]
        if not value:
            raise ValueError("--tier=<name> got empty value")
        return value, argv[1:]
    return None, argv


def main() -> int:
    if not REQ_FILE.exists():
        log(f"requirements.txt not found at {REQ_FILE}")
        return 1
    try:
        tier, args = _parse_tier_flag(sys.argv[1:])
    except ValueError as e:
        log(str(e))
        return 2
    try:
        bootstrap()
        if tier is not None:
            bootstrap_tier(tier)
    except subprocess.CalledProcessError as e:
        log(f"Bootstrap failed (exit {e.returncode}): {' '.join(map(str, e.cmd))}")
        return 1
    except (OSError, ValueError, FileNotFoundError) as e:
        log(f"Bootstrap failed: {e}")
        return 1

    py = str(venv_python())
    if not args:
        print(py)
        return 0

    # Convenience: if the first remaining arg is a bare script name without
    # any path separator (e.g. "extract_pdf_pymupdf4llm.py"), resolve it
    # against SCRIPTS_DIR. This is how the canonical invocation pattern
    # documented in SKILL.md works — agent code can pass just the script
    # name. Applies after --tier stripping so `--tier mineru extract_*.py`
    # routes the script name correctly.
    first = args[0]
    if (
        first.endswith(".py")
        and not first.startswith("-")
        and "/" not in first
        and "\\" not in first
    ):
        candidate = SCRIPTS_DIR / first
        if candidate.is_file():
            args = [str(candidate), *args[1:]]

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
