#!/usr/bin/env python3
"""
bootstrap_popo.py — opt-in auto-setup of the MinerU-Popo stage-2 environment.

doc2kb keeps heavy ML deps strictly opt-in (the lightweight tier is the default).
This script exists so that *when you have explicitly opted in* — by invoking it
directly, or by setting DOC2KB_POPO_AUTO so postprocess_popo --auto-setup calls
it — the Popo checkout, its Python env, and its 4B model get set up for you,
instead of the manual clone/conda/hf-download dance in the Popo README.

It NEVER runs as part of the default doc2kb pipeline. No env + no explicit
invocation = this file does nothing.

Steps (all idempotent):
  1. popo_home = $DOC2KB_POPO_REPO, else <skill-state-dir>/popo/MinerU-Popo.
  2. git clone opendatalab/MinerU-Popo (shallow, branch master) if absent.
  3. Create a dedicated env at <popo_home>/.venv — priority uv → conda → venv,
     targeting Python 3.10 (Popo's pin). Popo's bash scripts call `python3` from
     PATH (no `conda activate`), so postprocess_popo prepends this env's bin/ to
     PATH when it runs them — a uv venv works fine.
  4. Install deps, platform-aware:
       - Linux: pip install -r <popo_home>/requirements.txt  (upstream CUDA set).
       - macOS: pip install -r requirements-popo-mac.txt  (MPS set — upstream's
         CUDA reqs won't install here, but Popo's `transformers` inference backend
         needs none of them). VERIFIED working on Apple Silicon: the 4.44B Qwen3-VL
         loads on MPS and sustains ~22 tok/s once resident (see step 4b).
     4b. macOS only: patch model_utils.py `device_map="auto"` → `{"": "mps"}`. With
         "auto", accelerate offloads part of the model to DISK (its MPS memory
         estimate is wrong) — slow, and empirically produced EMPTY generations.
         Pinning the whole 4.44B (≈9 GB bf16) onto MPS fixes both.
  5. Download the model via huggingface_hub.snapshot_download (resumable), unless
     --skip-model. ~16 GB on disk (fp32 4.44B sharded; ≈9 GB bf16 once loaded).
     Skipped when already complete (--force-model re-verifies).
  6. Patch post_processing/model_utils.py so the transformers backend resolves the
     model — its real form is os.environ.get("POPO_MODEL_PATH", "popo_model"); we
     pin the default to the downloaded dir (postprocess_popo also exports the env).
  7. Record the env python at <popo_home>/.doc2kb-popo-python (the sentinel
     postprocess_popo reads to PATH-inject the env).

CLI:
    bootstrap_popo.py [--popo-home <dir>] [--skip-model] [--force-model]

Exit codes: 0 ok; 2 missing prerequisite (git absent, etc.); 1 a step failed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from _common import emit_failure, emit_success, log as _common_log
from ensure_env import _skill_state_dir, have


POPO_GIT_URL = "https://github.com/opendatalab/MinerU-Popo.git"
POPO_BRANCH = "master"
HF_MODEL_REPO = "DreamEternal/MinerU-Popo"
MODEL_SUBDIR = "models/Mineru-Popo"
PY_SENTINEL = ".doc2kb-popo-python"

SCRIPTS_DIR = Path(__file__).resolve().parent
MAC_REQ = SCRIPTS_DIR / "requirements-popo-mac.txt"


def log(msg: str) -> None:
    _common_log(msg, prefix="popo bootstrap")


def _default_popo_home() -> Path:
    env = os.environ.get("DOC2KB_POPO_REPO")
    if env:
        return Path(env).expanduser().resolve()
    return (_skill_state_dir() / "popo" / "MinerU-Popo").resolve()


def _venv_python(env_dir: Path) -> Path:
    if os.name == "nt":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def _clone(popo_home: Path) -> None:
    if (popo_home / ".git").is_dir():
        log(f"Popo repo already present at {popo_home}")
        return
    if not have("git"):
        raise RuntimeError("git not found on PATH — install git, then re-run")
    popo_home.parent.mkdir(parents=True, exist_ok=True)
    log(f"Cloning {POPO_GIT_URL} (branch {POPO_BRANCH}) → {popo_home}")
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", POPO_BRANCH,
         POPO_GIT_URL, str(popo_home)],
        check=True,
    )


def _create_env(env_dir: Path) -> Path:
    py = _venv_python(env_dir)
    if py.exists():
        log(f"Popo env already exists at {env_dir}")
        return py
    env_dir.parent.mkdir(parents=True, exist_ok=True)
    if have("uv"):
        log(f"Creating Popo env via uv (python 3.10) at {env_dir}")
        subprocess.run(["uv", "venv", "--python", "3.10", str(env_dir)], check=True)
    elif have("conda"):
        log(f"Creating Popo env via conda --prefix (python 3.10) at {env_dir}")
        subprocess.run(
            ["conda", "create", "--prefix", str(env_dir),
             "python=3.10", "pip", "-y", "-q"],
            check=True,
        )
    else:
        log(f"Creating Popo env via `python -m venv` at {env_dir} "
            "(uv/conda not found — interpreter may not be 3.10)")
        subprocess.run([sys.executable, "-m", "venv", str(env_dir)], check=True)
    return py


def _pip_install(py: Path, pip_args: list[str]) -> None:
    if have("uv"):
        subprocess.run(["uv", "pip", "install", "--python", str(py), *pip_args],
                       check=True)
    else:
        subprocess.run([str(py), "-m", "pip", "install",
                        "--disable-pip-version-check", *pip_args], check=True)


def _is_cuda_box() -> bool:
    return have("nvidia-smi") or Path("/proc/driver/nvidia").exists()


def _install_deps(py: Path, popo_home: Path) -> str:
    if sys.platform == "darwin":
        log("macOS / Apple Silicon detected. Upstream Popo requirements.txt is the "
            "heavy CUDA/vLLM serving stack (torch+cu12, nvidia-*, cupy, triton) and "
            "will NOT install here — but doc2kb runs Popo via its `transformers` "
            f"inference backend, which needs none of it. Installing {MAC_REQ.name} "
            "(torch MPS + transformers + the small real import surface). The 4.44B "
            "Qwen3-VL loads fully on MPS (after the device_map pin) and sustains "
            "~22 tok/s here. If an op is unimplemented on MPS, "
            "PYTORCH_ENABLE_MPS_FALLBACK=1 (set for you at run time) routes it to CPU.")
        if not MAC_REQ.is_file():
            raise RuntimeError(f"macOS requirements file missing: {MAC_REQ}")
        _pip_install(py, ["-r", str(MAC_REQ), "--upgrade"])
        return "mac-mps-transformers"
    req = popo_home / "requirements.txt"
    if not req.is_file():
        raise RuntimeError(f"Popo requirements.txt missing at {req}")
    if not _is_cuda_box():
        log("⚠ No NVIDIA GPU detected (no nvidia-smi). Popo's pinned requirements "
            "target CUDA 12 — the install may fail or end up CPU-only and very slow.")
    log(f"Installing Popo deps from {req} (large CUDA wheel set — several minutes)")
    _pip_install(py, ["-r", str(req)])
    _pip_install(py, ["huggingface_hub"])  # ensure the model-download API is present
    return "cuda-linux-upstream"


def _model_is_complete(model_dir: Path) -> bool:
    """True only when every shard named by the safetensors index is materialized
    and no `.incomplete` blob remains. A non-empty dir is NOT enough — a download
    interrupted (e.g. disk full) leaves complete shards + one `.incomplete`, which
    must NOT be mistaken for a finished model."""
    if not model_dir.is_dir():
        return False
    if any((model_dir / ".cache").rglob("*.incomplete")):
        return False
    index = model_dir / "model.safetensors.index.json"
    if index.is_file():
        try:
            weight_map = json.loads(index.read_text(encoding="utf-8"))["weight_map"]
        except (OSError, ValueError, KeyError):
            return False
        return all((model_dir / shard).is_file() for shard in set(weight_map.values()))
    # No shard index → single-file model; accept any materialized *.safetensors/*.bin.
    return any(model_dir.glob("*.safetensors")) or any(model_dir.glob("*.bin"))


def _download_model(py: Path, popo_home: Path, force: bool) -> Path:
    model_dir = popo_home / MODEL_SUBDIR
    if not force and _model_is_complete(model_dir):
        log(f"Model already complete at {model_dir} (use --force-model to re-verify)")
        return model_dir
    model_dir.mkdir(parents=True, exist_ok=True)
    log(f"Downloading {HF_MODEL_REPO} → {model_dir} (~16 GB sharded — resumable; "
        "snapshot_download skips complete shards by hash and finishes partial ones)")
    # snapshot_download is itself idempotent + resumable: it HEAD-checks every file
    # and only fetches missing/incomplete shards, so calling it on a partial dir
    # completes the download rather than restarting it.
    snippet = (
        "from huggingface_hub import snapshot_download; "
        f"snapshot_download(repo_id={HF_MODEL_REPO!r}, "
        f"local_dir={str(model_dir)!r})"
    )
    subprocess.run([str(py), "-c", snippet], check=True)
    return model_dir


def _patch_model_path(popo_home: Path, model_dir: Path) -> bool:
    """Best-effort, idempotent. Point Popo at the downloaded model. Never fatal —
    if the upstream anchor drifts, warn and let the user set it per the README."""
    target = str(model_dir)
    patched_any = False
    mu = popo_home / "post_processing" / "model_utils.py"
    if mu.is_file():
        text = mu.read_text(encoding="utf-8")
        # Real form upstream: os.environ.get("POPO_MODEL_PATH", "popo_model").
        # Patch the default so direct CLI runs resolve the model even without the
        # env var (postprocess_popo also exports POPO_MODEL_PATH at run time).
        new = re.sub(
            r'(os\.environ\.get\(\s*["\']POPO_MODEL_PATH["\']\s*,\s*)(["\'])[^"\']*\2',
            lambda m: m.group(1) + repr(target), text, count=1)
        # Also handle a bare `POPO_MODEL_PATH = "..."` assignment if a fork uses one.
        new = re.sub(r'(^\s*POPO_MODEL_PATH\s*=\s*)(["\'])[^"\']*\2',
                     lambda m: m.group(1) + repr(target), new, count=1, flags=re.M)
        if new != text:
            mu.write_text(new, encoding="utf-8")
            log(f"Patched POPO_MODEL_PATH default in {mu.name} → {target}")
            patched_any = True
    sh = popo_home / "scripts" / "run_inference.sh"
    if sh.is_file():
        stext = sh.read_text(encoding="utf-8")
        snew = re.sub(r'(MODEL_PATH=)"[^"]*"',
                      lambda m: m.group(1) + f'"{target}"', stext, count=1)
        if snew != stext:
            sh.write_text(snew, encoding="utf-8")
            log(f"Patched MODEL_PATH in scripts/{sh.name} → {target}")
            patched_any = True
    if not patched_any:
        log("Model-path anchors already point at the model (or absent). "
            "postprocess_popo also exports POPO_MODEL_PATH at run time.")
    return patched_any


def _patch_device_map_for_mac(popo_home: Path) -> bool:
    """darwin only, idempotent. Popo's model_utils.py hardcodes
    `device_map="auto"`, which on Apple Silicon makes accelerate offload part of
    the 4B model to DISK (the conservative MPS memory estimate is wrong) — that
    is slow AND empirically produced empty generations. Pin the whole model onto
    MPS instead (4.44B in bf16 ≈ 9 GB, fits comfortably on 16 GB+ unified memory).
    Best-effort: if the anchor drifts upstream, warn and move on."""
    mu = popo_home / "post_processing" / "model_utils.py"
    if not mu.is_file():
        return False
    text = mu.read_text(encoding="utf-8")
    new = re.sub(r'device_map\s*=\s*(["\'])auto\1',
                 'device_map={"": "mps"}', text, count=1)
    if new != text:
        mu.write_text(new, encoding="utf-8")
        log('Patched model_utils.py device_map="auto" → {"": "mps"} for Apple '
            "Silicon (avoids disk offload; whole 4B model resident on MPS)")
        return True
    if 'device_map={"": "mps"}' in text:
        return True  # already patched
    log('⚠ Could not find device_map="auto" in model_utils.py to pin to MPS — '
        "if generations come back empty, accelerate may be disk-offloading.")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Auto-setup the opt-in MinerU-Popo stage-2 environment "
                    "(clone + env + model). Heavy: downloads a ~16 GB model."
    )
    ap.add_argument("--popo-home", default=None,
                    help="where to clone/build Popo (default: "
                         "$DOC2KB_POPO_REPO or <state-dir>/popo/MinerU-Popo)")
    ap.add_argument("--skip-model", action="store_true",
                    help="set up repo+env but skip the ~16 GB model download")
    ap.add_argument("--force-model", action="store_true",
                    help="re-download the model even if already present")
    args = ap.parse_args()

    try:
        popo_home = (Path(args.popo_home).expanduser().resolve()
                     if args.popo_home else _default_popo_home())
        _clone(popo_home)
        env_python = _create_env(popo_home / ".venv")
        dep_profile = _install_deps(env_python, popo_home)
        if sys.platform == "darwin":
            _patch_device_map_for_mac(popo_home)
        model_dir: Path | None = None
        if not args.skip_model:
            model_dir = _download_model(env_python, popo_home, args.force_model)
            _patch_model_path(popo_home, model_dir)
        (popo_home / PY_SENTINEL).write_text(str(env_python) + "\n", encoding="utf-8")
    except subprocess.CalledProcessError as e:
        emit_failure(f"bootstrap step failed (exit {e.returncode}): "
                     f"{' '.join(map(str, e.cmd))}")
        return 1
    except RuntimeError as e:
        emit_failure(str(e))
        return 2
    except OSError as e:
        emit_failure(f"bootstrap failed: {e}")
        return 1

    emit_success(popo_home / PY_SENTINEL, "", warnings=(), extra={
        "popo_home": str(popo_home),
        "env_python": str(env_python),
        "dep_profile": dep_profile,
        "model_dir": str(model_dir) if model_dir else None,
        "platform": sys.platform,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
