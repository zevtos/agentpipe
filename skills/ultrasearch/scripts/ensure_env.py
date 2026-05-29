#!/usr/bin/env python3
"""
ensure_env.py — изолированное окружение для скилла ultrasearch.

Расположение скрипта: <skill_dir>/scripts/ensure_env.py
Расположение venv+data: глобально, вне кода скилла (переживает reinstall, ADR-008):
                      $ULTRASEARCH_HOME | $AGENTPIPE_HOME/ultrasearch |
                      ${XDG_DATA_HOME:-~/.local/share}/agentpipe/ultrasearch/{venv,data}
                      (Windows: %LOCALAPPDATA%\\agentpipe\\ultrasearch\\...).
                      Ключ — имя скилла, поэтому claude- и codex-таргеты делят venv+corpus.

Зачем это:
    Скилл тянет тяжёлые зависимости (torch ~150 MB на arm64, sentence-transformers,
    pymupdf4llm, sqlite-vec) — ставить их в системный Python неприемлемо. Этот
    скрипт создаёт изолированное окружение рядом со скиллом по фиксированному
    пути и поддерживает его при апдейтах через хэш requirements.txt.

Поведение:
    1. Считает sha256 от scripts/requirements.txt.
    2. Если venv существует и его .installed_hash совпадает — мгновенный no-op.
    3. Иначе создаёт venv (uv → conda --prefix → python -m venv) и ставит deps.
    4. Записывает свежий хэш в .venv/.installed_hash.

Использование:
    python3 ensure_env.py                  # bootstrap + вывести путь к venv-питону
    python3 ensure_env.py <script> [args]  # bootstrap + запустить script venv-питоном

Зависит только от stdlib. Поддерживает Python 3.8+.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path


SKILL_NAME = "ultrasearch"
SKILL_HOME_VAR = "ULTRASEARCH_HOME"

SKILL_DIR = Path(__file__).resolve().parent.parent   # installed code dir (volatile)
SCRIPTS_DIR = Path(__file__).resolve().parent
REQ_FILE = SCRIPTS_DIR / "requirements.txt"
PTH_NAME = "ultrasearch.pth"


def _skill_state_dir() -> Path:
    """Durable per-skill state root (venv + data/corpus.db), OUTSIDE the volatile
    code dir so re-install / update / multi-target install never wipe it.
    Keyed by skill NAME, so every install target (~/.claude, ~/.codex, ...)
    converges on one dir. Precedence (highest first):
        $ULTRASEARCH_HOME (verbatim leaf) → $AGENTPIPE_HOME/<name>
        → ${XDG_DATA_HOME:-~/.local/share}/agentpipe/<name>   (POSIX)
        → %LOCALAPPDATA%\\agentpipe\\<name>                    (Windows, non-roaming)
    Stdlib-only — runs before the venv exists. See ADR-008.
    DUP: path math kept byte-identical with _common.py:_skill_state_dir()."""
    per_skill = os.environ.get(SKILL_HOME_VAR)
    if per_skill:
        return Path(per_skill).expanduser()
    root = os.environ.get("AGENTPIPE_HOME")
    if not root:
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            base = xdg
        elif os.name == "nt":
            base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        else:
            base = str(Path.home() / ".local" / "share")
        root = str(Path(base) / "agentpipe")
    return Path(root).expanduser() / SKILL_NAME


STATE_DIR = _skill_state_dir()
VENV_DIR = STATE_DIR / "venv"
DATA_DIR = STATE_DIR / "data"
HASH_FILE = VENV_DIR / ".installed_hash"
LOCK_FILE = STATE_DIR / ".venv.lock"

# Legacy in-skill locations (pre-ADR-008) — migrated away on next bootstrap.
_LEGACY_VENV = SKILL_DIR / ".venv"
_LEGACY_LOCK = SKILL_DIR / ".venv.lock"
_LEGACY_DATA = SKILL_DIR / "data"


def log(msg: str) -> None:
    """Прогресс на stderr — чтобы stdout оставался чистым (там путь к питону)."""
    print(f"[ultrasearch env] {msg}", file=sys.stderr, flush=True)


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
    """Пишем <venv>/<purelib>/ultrasearch.pth с абсолютным путём к scripts/.
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


def _has_legacy_state() -> bool:
    """True while pre-ADR-008 in-skill state still exists and needs migrating."""
    return (_LEGACY_VENV.exists() or _LEGACY_LOCK.exists()
            or (_LEGACY_DATA / "corpus.db").exists())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_move_file(src: Path, dst: Path) -> None:
    """Move one file src→dst. Same-filesystem rename is atomic; a cross-fs move
    copies to a .partial sibling, verifies sha256, atomically renames into
    place, then unlinks the source — so an interrupted move never leaves a
    half-written file at the final name (which the 'new exists' guard trusts)."""
    try:
        os.rename(src, dst)
        return
    except OSError:
        pass  # cross-device — fall through to verified copy
    tmp = dst.with_name(dst.name + ".partial")
    shutil.copy2(src, tmp)
    if _sha256(tmp) != _sha256(src):
        tmp.unlink(missing_ok=True)
        raise OSError(f"checksum mismatch copying {src} -> {dst}")
    os.replace(tmp, dst)
    src.unlink()


def _migrate_corpus(legacy_data: "Path | None" = None) -> None:
    """Relocate a legacy in-skill corpus.db to DATA_DIR. MOVE, never rebuild —
    it is irreplaceable user data. Never overwrites an existing new corpus.
    `legacy_data` overrides the source dir; the installer passes it via
    --migrate-from to move the corpus out BEFORE it removes the old skill dir
    (runtime migration alone is too late — the installer's rm wins the race)."""
    legacy = legacy_data if legacy_data is not None else _LEGACY_DATA
    legacy_db = legacy / "corpus.db"
    new_db = DATA_DIR / "corpus.db"
    if not legacy_db.exists():
        return
    if new_db.exists():
        log(f"Legacy corpus at {legacy_db} left untouched — a corpus already "
            f"exists at {new_db}. Delete the legacy copy manually if it is stale.")
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Fold the WAL into the main db so a single-file move stays consistent.
    try:
        import sqlite3
        con = sqlite3.connect(str(legacy_db))
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
    except Exception as e:  # best-effort; sidecars are moved too just in case
        log(f"WAL checkpoint on legacy corpus failed ({e}); moving files as-is")
    try:
        _safe_move_file(legacy_db, new_db)
    except OSError as e:
        log(f"Corpus migration failed ({e}); legacy corpus left at {legacy_db}")
        return
    for sidecar in ("corpus.db-wal", "corpus.db-shm"):
        src = legacy / sidecar
        if src.exists():
            try:
                _safe_move_file(src, DATA_DIR / sidecar)
            except OSError:
                src.unlink(missing_ok=True)  # stale post-checkpoint; safe to drop
    legacy_cache = legacy / "cache"
    if legacy_cache.is_dir() and not (DATA_DIR / "cache").exists():
        try:
            os.rename(legacy_cache, DATA_DIR / "cache")
        except OSError:
            shutil.copytree(legacy_cache, DATA_DIR / "cache", dirs_exist_ok=True)
            shutil.rmtree(legacy_cache, ignore_errors=True)
    log(f"Migrated corpus → {new_db}")


def _migrate_legacy_state() -> None:
    """One-time relocation of pre-ADR-008 in-skill state, under the lock.
    corpus.db is MOVED (irreplaceable); the non-relocatable venv is removed and
    rebuilt at STATE_DIR by bootstrap(). Idempotent."""
    _migrate_corpus()  # data first — irreplaceable
    had_venv = _LEGACY_VENV.exists()
    for legacy in (_LEGACY_VENV, _LEGACY_LOCK):
        try:
            if legacy.is_dir():
                shutil.rmtree(legacy, ignore_errors=True)
            elif legacy.exists():
                legacy.unlink()
        except OSError as e:
            log(f"Could not remove legacy {legacy}: {e}")
    if had_venv and not _LEGACY_VENV.exists():
        log(f"Migrated to global state layout (ADR-008): legacy in-skill venv "
            f"removed; a fresh venv will be built at {VENV_DIR}")


def bootstrap() -> None:
    if not needs_update() and not _has_legacy_state():
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
        _migrate_legacy_state()
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
    # Install-time hook: relocate a legacy in-skill corpus to the global state
    # dir BEFORE the installer removes the old skill dir (runtime migration
    # alone loses the race against the installer's rm -rf). Stdlib-only, no venv
    # build; safe to run repeatedly. Used by install.sh / install.ps1.
    if len(sys.argv) >= 3 and sys.argv[1] == "--migrate-from":
        lock = _acquire_lock()
        try:
            _migrate_corpus(Path(sys.argv[2]).expanduser() / "data")
        finally:
            _release_lock(lock)
        return 0
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

    # Convenience: if the first arg is a bare script name without any path
    # separator (e.g. "discover.py"), resolve it against SCRIPTS_DIR. This is
    # how the canonical invocation pattern documented in SKILL.md works.
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
