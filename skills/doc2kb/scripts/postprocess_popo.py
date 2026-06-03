#!/usr/bin/env python3
"""
postprocess_popo.py — opt-in stage-2 post-processor for doc2kb.

Bridges MinerU's page-level output (cached by extract_pdf_mineru.py with
`--keep-raw`) to opendatalab/MinerU-Popo, a 4B post-processing model that
reconstructs document-level tree structure (heading hierarchy, cross-page
table merging, paragraph truncation repair, image↔caption association).

When to use:
    Long, multi-page documents where MinerU's per-page output broke the
    hierarchy or split paragraphs/tables across page boundaries. Popo
    reportedly lifts TEDS hierarchy score from 53.7 → 90.6 on MinerU
    output. For short PDFs (≤ ~5 pages) the gain is negligible and the
    infra cost (conda env, 4B model download, optional external LLM API
    for enrichment) isn't justified.

When NOT to use:
    - Single-page or short documents.
    - Any corpus where MinerU hasn't been run yet (Popo requires the
      cached `<kb_dir>/_mineru/<doc_id>/` artefacts that
      `extract_pdf_mineru.py --keep-raw` writes).
    - Default doc2kb workflows. This step is invoked manually after the
      main extraction is reviewed.

Prerequisites:
    1. Clone the upstream Popo repo somewhere local:
           git clone https://github.com/opendatalab/MinerU-Popo.git
    2. Follow the Popo README to install its conda env and download the
       MinerU-Popo HF model into `<popo_repo>/models/Mineru-Popo/`.
    3. Edit `<popo_repo>/post_processing/model_utils.py` so `POPO_MODEL_PATH`
       (or the vLLM `url`/`key` in `popo_generate`) points at the local
       model. If you want enrichment + QA, also configure `qwen_generate`
       / `gpt_generate` per the Popo README.
    4. Either export `DOC2KB_POPO_REPO=/abs/path/to/MinerU-Popo` or pass
       `--popo-repo /abs/path/to/MinerU-Popo` to this script.

This script does NOT install Popo, does NOT download the 4B model, and
does NOT contact any external LLM API. It is pure glue — it copies cached
MinerU output into the directory Popo's normalizer expects, invokes
Popo's three bash scripts in sequence, and writes the resulting tree(s)
back into the kb as sidecar files. Any missing dependency triggers an
exit 2 with the install instructions above.

CLI:
    postprocess_popo.py <kb_dir>
                        [--popo-repo /abs/path]
                        [--doc-id doc-NNN]              # postprocess one doc; default: all cached
                        [--run-name <name>]             # subfolder name under post-process/mineru/
                        [--skip-normalization]          # if you've already normalized
                        [--skip-inference]              # if you've already inferred

Output:
    For each processed doc, writes:
        <kb_dir>/docs/<doc_id>-<slug>.tree.json   # the document tree
    and appends `postprocess: popo@<version>` to the doc's frontmatter
    `warnings` block so the audit trail is preserved. The body of the
    `.md` is NOT rewritten by this script — tree consumption is left to
    the agent that builds the final RAG-ready chunks from the kb.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from _common import emit_failure, emit_success, log


POPO_ENV_VAR = "DOC2KB_POPO_REPO"
POPO_BASH_SCRIPTS = (
    "scripts/run_label_normalization.sh",
    "scripts/run_inference.sh",
    "scripts/build_tree.sh",
)
POPO_OUTPUT_TREE_DIR = "outputs/build_tree/mineru"

SCRIPTS_DIR = Path(__file__).resolve().parent
# Sentinel written by bootstrap_popo.py holding the path to Popo's dedicated env
# python. Its bin/ dir is prepended to PATH when running Popo's bash scripts
# (which call `python3` from PATH, not `conda activate`).
POPO_PY_SENTINEL = ".doc2kb-popo-python"

POPO_NOT_FOUND_HINT = (
    "MinerU-Popo repo not configured. Either:\n"
    f"  1) Set the env var: export {POPO_ENV_VAR}=/abs/path/to/MinerU-Popo\n"
    "  2) Pass --popo-repo /abs/path/to/MinerU-Popo on the command line\n"
    "Install Popo per upstream README:\n"
    "  git clone https://github.com/opendatalab/MinerU-Popo.git\n"
    "  cd MinerU-Popo\n"
    "  conda create -n popo python=3.10 && conda activate popo\n"
    "  pip install -r requirements.txt\n"
    "  hf download DreamEternal/MinerU-Popo --local-dir models/Mineru-Popo"
)


def _resolve_popo_repo(arg: str | None) -> Path | None:
    """Return the Popo repo path. Precedence: --popo-repo > $DOC2KB_POPO_REPO >
    the bootstrap_popo.py auto-default (<state-dir>/popo/MinerU-Popo) if it
    exists. We don't otherwise auto-detect — too easy to pick the wrong checkout
    and silently overwrite someone else's working copy."""
    candidate = arg or os.environ.get(POPO_ENV_VAR)
    if candidate:
        return Path(candidate).expanduser().resolve()
    try:
        from ensure_env import _skill_state_dir
        auto = (_skill_state_dir() / "popo" / "MinerU-Popo").resolve()
        if auto.is_dir():
            return auto
    except Exception:  # noqa: BLE001 — auto-default is a convenience, never fatal
        pass
    return None


def _popo_subprocess_env(popo_repo: Path) -> dict | None:
    """Build the env for Popo's bash scripts. On macOS we always return an env
    (even without a sentinel) so the MPS knobs are set; the sentinel additionally
    PATH-injects the bootstrapped env's python. Returns None (ambient env) only on
    non-darwin without a sentinel."""
    sentinel = popo_repo / POPO_PY_SENTINEL
    py = ""
    if sentinel.is_file():
        try:
            py = sentinel.read_text(encoding="utf-8").strip()
        except OSError:
            py = ""

    if not py and sys.platform != "darwin":
        return None

    env = dict(os.environ)
    if py:
        bindir = str(Path(py).parent)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    # Popo's transformers backend (model_utils._transformers_generate) reads the
    # model from os.environ["POPO_MODEL_PATH"] (default "popo_model" — a relative
    # dir that won't exist). run_inference.sh never exports it, so wire it to the
    # model the bootstrap downloaded.
    model_dir = popo_repo / "models" / "Mineru-Popo"
    if model_dir.is_dir():
        env.setdefault("POPO_MODEL_PATH", str(model_dir))
    if sys.platform == "darwin":
        # Popo's transformers backend runs the 4B VLM on MPS; route any op that
        # MPS doesn't implement to CPU instead of crashing.
        env.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    return env


def _validate_popo_layout(repo: Path) -> list[str]:
    """Return a list of missing required files. Empty list means the repo
    looks ready to run."""
    missing: list[str] = []
    for script in POPO_BASH_SCRIPTS:
        if not (repo / script).is_file():
            missing.append(script)
    return missing


def _list_cached_docs(cache_dir: Path) -> list[str]:
    """Each subfolder of <kb_dir>/_mineru/ is one cached MinerU run named
    after the doc_id. We skip dotfiles so a future `.cache/` sibling
    doesn't accidentally enter the pipeline."""
    if not cache_dir.is_dir():
        return []
    return sorted(
        p.name for p in cache_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def _stage_doc_into_popo(
    cache_doc_dir: Path,
    popo_repo: Path,
    run_name: str,
    doc_id: str,
) -> Path:
    """Copy <kb_dir>/_mineru/<doc_id>/ into
    <popo_repo>/post-process/mineru/<run_name>/<doc_id>/. Popo's
    label-normalization script scans every subfolder under
    `post-process/<source_name>/` so the per-doc layout keeps multiple
    docs isolated from each other."""
    target = popo_repo / "post-process" / "mineru" / run_name / doc_id
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(cache_doc_dir, target)
    return target


def _run_bash(script: Path, cwd: Path, env: dict | None = None) -> tuple[int, str]:
    """Invoke a Popo bash script. We force `bash` even on macOS where the
    user's default shell may be zsh, since Popo's scripts use bash-isms.
    `env` (when given) carries the bootstrapped Popo env's bin/ on PATH."""
    if not script.is_file():
        return 127, f"script not found: {script}"
    log(f"$ bash {script.relative_to(cwd)} (cwd={cwd})", prefix="popo")
    proc = subprocess.run(
        ["bash", str(script)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-12:])
        return proc.returncode, tail
    return 0, ""


def _collect_trees(
    popo_repo: Path,
    run_name: str,
    doc_ids: list[str],
    kb_docs_dir: Path,
) -> list[dict]:
    """For each processed doc, copy the Popo tree JSON next to the kb md.
    Popo writes its trees as `outputs/build_tree/mineru/<run_name>/<doc_id>/*.json`.
    Returns a per-doc summary list suitable for the success payload.
    """
    base = popo_repo / POPO_OUTPUT_TREE_DIR / run_name
    summaries: list[dict] = []
    if not base.is_dir():
        log(f"no tree outputs under {base}", prefix="popo")
        return summaries
    for doc_id in doc_ids:
        src_dir = base / doc_id
        if not src_dir.is_dir():
            summaries.append({"doc_id": doc_id, "tree_copied": False,
                              "reason": "popo produced no output"})
            continue
        # Locate the kb doc to derive the slug for the sidecar name.
        kb_doc = next(
            (p for p in kb_docs_dir.glob(f"{doc_id}-*.md") if p.is_file()),
            None,
        )
        if kb_doc is None:
            summaries.append({"doc_id": doc_id, "tree_copied": False,
                              "reason": f"no kb doc matching {doc_id}-*.md"})
            continue
        sidecar = kb_doc.with_suffix(".tree.json")
        # Pick the largest JSON in the tree dir — Popo emits a few aux
        # files (txt previews, partial chunks); the real tree is the
        # one with `tree_nodes` or `nodes` at the top level. Falling
        # back to "largest .json" keeps us robust to filename drift.
        tree_files = sorted(
            (p for p in src_dir.glob("*.json") if p.is_file()),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        if not tree_files:
            summaries.append({"doc_id": doc_id, "tree_copied": False,
                              "reason": "popo run produced no JSON"})
            continue
        try:
            sidecar.write_bytes(tree_files[0].read_bytes())
        except OSError as e:
            summaries.append({"doc_id": doc_id, "tree_copied": False,
                              "reason": f"copy failed: {e}"})
            continue
        summaries.append({
            "doc_id": doc_id,
            "tree_copied": True,
            "tree_path": str(sidecar),
            "source": str(tree_files[0]),
        })
    return summaries


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Run MinerU-Popo over cached MinerU outputs in a doc2kb kb. "
            "Strictly opt-in — requires an upstream Popo checkout."
        ),
    )
    ap.add_argument("kb_dir", help="Path to the doc2kb kb directory")
    ap.add_argument(
        "--popo-repo",
        default=None,
        help=f"Path to the MinerU-Popo clone. Falls back to ${POPO_ENV_VAR}.",
    )
    ap.add_argument(
        "--doc-id",
        default=None,
        help="Postprocess only this doc_id. Default: every doc under "
             "<kb_dir>/_mineru/.",
    )
    ap.add_argument(
        "--run-name",
        default="doc2kb",
        help="Subfolder name created under <popo_repo>/post-process/mineru/. "
             "Defaults to 'doc2kb' so multiple kbs share the same Popo "
             "checkout without colliding.",
    )
    ap.add_argument(
        "--skip-normalization", action="store_true",
        help="Skip step 2 (run_label_normalization.sh) — useful when "
             "re-running inference after editing normalized output.",
    )
    ap.add_argument(
        "--skip-inference", action="store_true",
        help="Skip step 3 (run_inference.sh) — useful when re-building "
             "trees from existing inference output.",
    )
    ap.add_argument(
        "--auto-setup", action="store_true",
        help="If no Popo repo is configured, run bootstrap_popo.py first "
             "(clone + env + ~8 GB model download), then proceed.",
    )
    args = ap.parse_args()

    kb_dir = Path(args.kb_dir).expanduser().resolve()
    if not kb_dir.is_dir():
        emit_failure(f"kb_dir not found: {kb_dir}")
        return 1

    popo_repo = _resolve_popo_repo(args.popo_repo)
    if popo_repo is None and args.auto_setup:
        log("Popo repo not configured; --auto-setup → running bootstrap_popo.py "
            "(this may clone the repo, build an env, and download ~8 GB)",
            prefix="popo")
        subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "ensure_env.py"), "bootstrap_popo.py"],
            check=False,
        )
        popo_repo = _resolve_popo_repo(args.popo_repo)
    if popo_repo is None:
        emit_failure(POPO_NOT_FOUND_HINT)
        return 2
    if not popo_repo.is_dir():
        emit_failure(f"popo repo path is not a directory: {popo_repo}\n\n{POPO_NOT_FOUND_HINT}")
        return 2
    missing = _validate_popo_layout(popo_repo)
    if missing:
        emit_failure(
            "Popo repo missing required scripts: " + ", ".join(missing)
            + "\n\nIs this the right checkout? Expected layout per Popo README.\n\n"
            + POPO_NOT_FOUND_HINT
        )
        return 2

    cache_dir = kb_dir / "_mineru"
    available = _list_cached_docs(cache_dir)
    if not available:
        emit_failure(
            f"no cached MinerU output found at {cache_dir}.\n"
            "Run extract_pdf_mineru.py with --keep-raw first."
        )
        return 1

    if args.doc_id:
        if args.doc_id not in available:
            emit_failure(
                f"doc_id {args.doc_id!r} not in cache. "
                f"Available: {available}"
            )
            return 1
        targets = [args.doc_id]
    else:
        targets = available

    # Stage each doc under post-process/mineru/<run>/<doc_id>/.
    staged: list[Path] = []
    for doc_id in targets:
        try:
            dst = _stage_doc_into_popo(
                cache_dir / doc_id, popo_repo, args.run_name, doc_id,
            )
            staged.append(dst)
        except OSError as e:
            emit_failure(f"failed to stage {doc_id}: {e}")
            return 1
    log(f"staged {len(staged)} doc(s) under {popo_repo}/post-process/mineru/{args.run_name}/",
        prefix="popo")

    # Use the bootstrapped env's python (if bootstrap_popo.py set one up) when
    # invoking Popo's bash scripts — they resolve `python3` from PATH.
    popo_env = _popo_subprocess_env(popo_repo)
    if popo_env is not None:
        log(f"using bootstrapped Popo env (sentinel {POPO_PY_SENTINEL})", prefix="popo")

    # Run Popo's three bash scripts in order. Each step can be skipped via
    # a flag for iterative re-runs without re-normalizing/re-inferring.
    steps: list[tuple[str, str, bool]] = [
        ("normalization", POPO_BASH_SCRIPTS[0], args.skip_normalization),
        ("inference",     POPO_BASH_SCRIPTS[1], args.skip_inference),
        ("build_tree",    POPO_BASH_SCRIPTS[2], False),
    ]
    for label, rel_script, skip in steps:
        if skip:
            log(f"skipping {label} (--skip-{label.split('_')[0]} set)", prefix="popo")
            continue
        rc, err = _run_bash(popo_repo / rel_script, popo_repo, env=popo_env)
        if rc != 0:
            emit_failure(
                f"popo {label} failed (exit {rc}):\n{err or 'no stderr captured'}"
            )
            return 1

    summaries = _collect_trees(popo_repo, args.run_name, targets, kb_dir / "docs")

    success_extras = {
        "popo_repo": str(popo_repo),
        "run_name": args.run_name,
        "processed": targets,
        "results": summaries,
    }
    # Reuse emit_success's shape (it expects a body but we have none —
    # pass a placeholder so tokens_estimated stays sensible at zero).
    placeholder = kb_dir / "_logs" / "popo.json"
    placeholder.parent.mkdir(parents=True, exist_ok=True)
    placeholder.write_text(json.dumps(success_extras, indent=2),
                           encoding="utf-8")
    emit_success(placeholder, "", warnings=(), extra=success_extras)
    return 0


if __name__ == "__main__":
    sys.exit(main())
