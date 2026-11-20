#!/usr/bin/env python3
"""
ensure_env.py — изолированное окружение для скилла doc2kb.

Расположение скрипта: <skill_dir>/scripts/ensure_env.py
Расположение venv:    глобально, вне кода скилла (переживает reinstall, ADR-008):
                      $DOC2KB_HOME | $AGENTPIPE_HOME/doc2kb |
                      ${XDG_DATA_HOME:-~/.local/share}/agentpipe/doc2kb/venv
                      (Windows: %LOCALAPPDATA%\\agentpipe\\doc2kb\\venv).
                      Ключ — имя скилла, поэтому claude- и codex-таргеты делят venv.

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
    python3 ensure_env.py --tier transcribe        # speech-to-text для аудио/видео (свой venv)

Тиры (opt-in):
    lightweight (default) — requirements.txt, всегда поставлен.
    mineru                — requirements-mineru.txt, отдельный хэш-файл
                            (.installed_hash_mineru), additive, ставится только
                            при явном --tier mineru. ~3 GB + MLX wheels на Mac.
    transcribe            — requirements-transcribe.txt в СОБСТВЕННОМ venv
                            (<state>/transcribe/venv, Python 3.12): GigaAM пинит
                            onnx/onnxruntime ниже, чем нужно mineru, а torch
                            отстаёт от свежего CPython. После pip-установки
                            `_asr.py setup` качает закреплённые модели (whisper.cpp
                            ggml + VAD, либо mlx/faster-whisper; GigaAM) в
                            <state>/transcribe/models. ~1 GB deps + 3.5 GB моделей.

Зависит только от stdlib — иначе курица-яйцо. Поддерживает Python 3.8+.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path


SKILL_NAME = "doc2kb"
SKILL_HOME_VAR = "DOC2KB_HOME"

SKILL_DIR = Path(__file__).resolve().parent.parent   # installed code dir (volatile)
SCRIPTS_DIR = Path(__file__).resolve().parent
REQ_FILE = SCRIPTS_DIR / "requirements.txt"
PTH_NAME = "doc2kb.pth"


def _skill_state_dir() -> Path:
    """Durable per-skill state root (venv + sentinels), OUTSIDE the volatile
    code dir so re-install / update / multi-target install never wipe it.
    Keyed by skill NAME, so every install target (~/.claude, ~/.codex, ...)
    converges on one dir. Precedence (highest first):
        $DOC2KB_HOME (verbatim leaf) → $AGENTPIPE_HOME/<name>
        → ${XDG_DATA_HOME:-~/.local/share}/agentpipe/<name>   (POSIX)
        → %LOCALAPPDATA%\\agentpipe\\<name>                    (Windows, non-roaming)
    Stdlib-only — runs before the venv exists. See ADR-008.
    DUP: path math is intentionally simple; no other file in this skill needs it."""
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
HASH_FILE = VENV_DIR / ".installed_hash"
LOCK_FILE = STATE_DIR / ".venv.lock"

# Legacy in-skill locations (pre-ADR-008) — migrated away on next bootstrap.
_LEGACY_VENV = SKILL_DIR / ".venv"
_LEGACY_LOCK = SKILL_DIR / ".venv.lock"

# Opt-in tiers layered on top of the lightweight base. Adding a tier never
# touches the lightweight install — they live in the same venv but get their
# own hash file so re-running `--tier mineru` is idempotent and switching
# back to default invocation doesn't trigger a reinstall.
TIERS: dict[str, str] = {
    "mineru": "requirements-mineru.txt",
}

# Opt-in tiers that get their OWN venv under <state>/<tier>/venv because their
# pins clash with the shared one (GigaAM: onnx 1.19 / onnxruntime 1.23 vs the
# mineru tier's newer onnxruntime; torch wheels trail the newest CPython the
# shared venv may run on). Value: (requirements file, Python version for the
# venv, post-install setup script run as `<venv-python> <script> setup
# --models-dir <state>/<tier>/models`, or None). Scripts in the shared venv
# reach the tier as a subprocess (see isolated_python()).
ISOLATED_TIERS: dict[str, tuple[str, str, str | None]] = {
    "transcribe": ("requirements-transcribe.txt", "3.12", "_asr.py"),
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
    if tier in ISOLATED_TIERS:
        return SCRIPTS_DIR / ISOLATED_TIERS[tier][0]
    if tier not in TIERS:
        raise ValueError(
            f"unknown tier {tier!r}; available: {sorted(set(TIERS) | set(ISOLATED_TIERS))}"
        )
    return SCRIPTS_DIR / TIERS[tier]


def isolated_venv_dir(tier: str) -> Path:
    return STATE_DIR / tier / "venv"


def isolated_models_dir(tier: str) -> Path:
    return STATE_DIR / tier / "models"


def isolated_python(tier: str) -> Path:
    v = isolated_venv_dir(tier)
    return v / "Scripts" / "python.exe" if os.name == "nt" else v / "bin" / "python"


def _isolated_hash(tier: str) -> str:
    return hashlib.sha256(tier_req_file(tier).read_bytes()).hexdigest()


def isolated_ready(tier: str) -> bool:
    """True iff the tier's own venv exists, matches its requirements file and
    its post-install setup finished. Cheap (two small file reads) — callers in
    the shared venv use it to decide `needs_install` without importing torch."""
    v = isolated_venv_dir(tier)
    try:
        current = _isolated_hash(tier)
        return (isolated_python(tier).exists()
                and (v / ".installed_hash").read_text().strip() == current
                and (v / ".setup_done").read_text().strip() == current)
    except OSError:
        return False


def _find_python(version: str) -> str | None:
    for name in (f"python{version}", "python3", "python"):
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            out = subprocess.run([exe, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                                 capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if out == version:
            return exe
    return None


def bootstrap_isolated_tier(tier: str) -> None:
    """Build/refresh the tier's own venv, then run its setup script once per
    requirements hash. Idempotent; a no-op when `isolated_ready(tier)`."""
    req_name, py_version, setup_script = ISOLATED_TIERS[tier]
    req = SCRIPTS_DIR / req_name
    if not req.exists():
        raise FileNotFoundError(f"tier {tier!r} requirements file missing: {req}")
    bootstrap()  # the shared lightweight venv runs the extractors that call the tier
    if isolated_ready(tier):
        return
    venv = isolated_venv_dir(tier)
    py = isolated_python(tier)
    lock = _acquire_lock()
    try:
        if isolated_ready(tier):
            return
        current = _isolated_hash(tier)
        hash_file = venv / ".installed_hash"
        if not (py.exists() and hash_file.exists() and hash_file.read_text().strip() == current):
            venv.parent.mkdir(parents=True, exist_ok=True)
            if not py.exists():
                if have("uv"):
                    log(f"Creating tier {tier!r} venv (Python {py_version}) via uv at {venv}")
                    subprocess.run(["uv", "venv", "--python", py_version, str(venv)], check=True)
                else:
                    exe = _find_python(py_version)
                    if exe is None:
                        raise FileNotFoundError(
                            f"tier {tier!r} needs Python {py_version}: install uv "
                            f"(https://docs.astral.sh/uv/) or python{py_version}")
                    log(f"Creating tier {tier!r} venv via {exe} -m venv at {venv}")
                    subprocess.run([exe, "-m", "venv", str(venv)], check=True)
            log(f"Installing opt-in tier {tier!r} from {req.name} (several minutes — torch + "
                "speech models' runtimes)")
            if have("uv"):
                subprocess.run(["uv", "pip", "install", "--python", str(py), "-r", str(req),
                                "--upgrade"], check=True)
            else:
                subprocess.run([str(py), "-m", "pip", "install", "-r", str(req), "--upgrade",
                                "--quiet", "--disable-pip-version-check"], check=True)
            hash_file.write_text(current)
        if setup_script:
            log(f"Tier {tier!r}: fetching pinned models into {isolated_models_dir(tier)} "
                "(skips what is already there)")
            rc = subprocess.run([str(py), str(SCRIPTS_DIR / setup_script), "setup",
                                 "--models-dir", str(isolated_models_dir(tier))]).returncode
            if rc != 0:
                raise RuntimeError(f"tier {tier!r} setup failed (exit {rc}) — see the log above")
        (venv / ".setup_done").write_text(current)
        log(f"Tier {tier!r} ready")
    finally:
        _release_lock(lock)


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
    # Cached in <venv>/.purelib: the subprocess probe costs ~18ms and is paid
    # on every dispatch (extract_corpus runs ensure_env once per file). The
    # cache dies with VENV_DIR on rebuild; is_dir() rejects a stale path.
    py = venv_python()
    if not py.exists():
        return None
    cache = VENV_DIR / ".purelib"
    try:
        cached = cache.read_text(encoding="utf-8").strip()
        if cached and Path(cached).is_dir():
            return Path(cached)
    except OSError:
        pass
    try:
        out = subprocess.run(
            [str(py), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None
    if not out:
        return None
    try:
        cache.write_text(out + "\n", encoding="utf-8")
    except OSError:
        pass
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


def pth_current() -> bool:
    """True iff the .pth exists AND points at THIS checkout's scripts/. One venv
    is shared by every checkout of the skill (the installed copy, the repo, git
    worktrees); checking mere existence let whichever checkout bootstrapped
    last keep the .pth, which then dangled once that checkout was removed."""
    sp = site_packages_dir()
    if sp is None:
        return False
    try:
        return (sp / PTH_NAME).read_text(encoding="utf-8") == str(SCRIPTS_DIR.resolve()) + "\n"
    except OSError:
        return False


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


def _has_legacy_state() -> bool:
    """True while pre-ADR-008 in-skill state still exists and needs migrating."""
    return _LEGACY_VENV.exists() or _LEGACY_LOCK.exists()


def _migrate_legacy_state() -> None:
    """One-time relocation of pre-ADR-008 in-skill state. The venv is
    non-relocatable (absolute shebangs / pyvenv.cfg home), so the legacy one is
    removed and a fresh venv is rebuilt at STATE_DIR by bootstrap() — cheap, the
    uv wheel cache and any model caches survive. Idempotent; runs under the lock.
    NOTE: an opt-in tier (e.g. mineru) is NOT auto-reinstalled — re-run
    `--tier <name>` after migration."""
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
        if not pth_current():
            lock = _acquire_lock()
            try:
                if not pth_current():
                    write_pth()
            finally:
                _release_lock(lock)
        return
    lock = _acquire_lock()
    try:
        _migrate_legacy_state()
        if not needs_update():
            if not pth_current():
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
    if tier in ISOLATED_TIERS:
        bootstrap_isolated_tier(tier)
        return
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
    # Install-time hook (uniform across skills): doc2kb has no durable data to
    # migrate — its venv is rebuilt at the global state dir on next bootstrap —
    # so accept --migrate-from and no-op, letting the installer call it uniformly.
    if len(sys.argv) >= 3 and sys.argv[1] == "--migrate-from":
        return 0
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
    except (OSError, ValueError, FileNotFoundError, RuntimeError) as e:
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

    # Prepend the venv's bin (or Scripts on Windows) to PATH so target
    # scripts can locate console entry points installed by their tier
    # (e.g. the `mineru` CLI from the opt-in mineru tier) via
    # shutil.which / subprocess without needing an absolute path.
    venv_bin = str(venv_python().parent)
    existing_path = os.environ.get("PATH", "")
    if venv_bin not in existing_path.split(os.pathsep):
        os.environ["PATH"] = (
            venv_bin + os.pathsep + existing_path if existing_path else venv_bin
        )

    if os.name == "nt":
        rc = subprocess.run([py, *args]).returncode
        return rc
    os.execv(py, [py, *args])
    return 0  # unreachable


if __name__ == "__main__":
    sys.exit(main())
