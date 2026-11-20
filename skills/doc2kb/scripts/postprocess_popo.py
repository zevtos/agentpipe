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
    infra cost (dedicated env, 16 GB model download) isn't justified.

When NOT to use:
    - Single-page or short documents.
    - Any corpus where MinerU hasn't been run yet (Popo requires the
      cached `<kb_dir>/_mineru/<doc_id>/` artefacts that
      `extract_pdf_mineru.py --keep-raw` writes).
    - Default doc2kb workflows. This step is invoked manually after the
      main extraction is reviewed.

Prerequisites:
    A MinerU-Popo checkout with its env and model — either
    `bootstrap_popo.py` (auto: clone, env, model, macOS MPS pin) or by hand
    per the upstream README (clone, env, `hf download DreamEternal/MinerU-Popo
    --local-dir models/Mineru-Popo`). The repo is found via `--popo-repo` >
    `$DOC2KB_POPO_REPO` > the bootstrap default `<state>/popo/MinerU-Popo`.

This script does NOT install Popo, does NOT download the model, and does NOT
contact any external LLM API. It is pure glue: it calls Popo's three Python
entry points with explicit paths, keeps every intermediate file in the kb,
and copies the resulting tree(s) next to the kb docs. Any missing dependency
triggers an exit 2 with install instructions.

How it drives Popo (upstream's bash wrappers are not used: they hard-code
`--pdf-dir eval_pdf_dir`, which crashes when that dir is absent, and
`--limit 5`, which silently drops documents):
    1. post_processing/label_normalization.py --model mineru
         --input-dir <kb>/_mineru --doc-id <ids…> --pdf-map-json …
       MinerU-Popo's MineruReader reads
       `<input-dir>/<doc_id>/vlm/<doc_id>_{model,middle,content_list}.json`,
       which is exactly the `_mineru/` cache layout, so nothing is staged.
       The pdf map points each doc at `_mineru/<doc_id>/<doc_id>.pdf` (the
       copy `--keep-raw` makes): Popo's inference renders those pages as
       the VLM's image input, and the PDF's stem names every output file.
    2. post_processing/run_inference.py --model mineru --limit 0
       (transformers backend, POPO_MAX_NEW_TOKENS=8192 like upstream's
       run_inference.sh; both overridable from the environment).
    3. post_processing/get_json_tree.py.
    Steps 2 and 3 consume a whole directory, so each gets a fresh selection
    dir holding only this run's docs.

Work dir (`<kb_dir>/_popo/`, kept between runs for --skip-*):
    pdf_map.json
    label_normalization/mineru/<doc_id>.json
    inference/<doc_id>.json
    build_tree/<doc_id>.json          # the tree
    build_tree_txt/<doc_id>.txt       # indented title|preview outline

CLI:
    postprocess_popo.py <kb_dir>
                        [--popo-repo /abs/path]
                        [--doc-id doc-NNN]              # postprocess one doc; default: all cached
                        [--skip-normalization]          # reuse _popo/label_normalization/
                        [--skip-inference]              # reuse _popo/inference/
                        [--auto-setup]                  # bootstrap Popo first when unconfigured

Output:
    For each processed doc, writes
        <kb_dir>/docs/<doc_id>-<slug>.tree.json   # copy of _popo/build_tree/<doc_id>.json
    The `.md` is NOT rewritten — tree consumption is left to the agent that
    builds the final RAG-ready chunks from the kb. Per-doc results (and the
    reason a doc produced no tree) are in the success JSON and in
    <kb_dir>/_logs/popo.json.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from _common import emit_failure, emit_success, log


POPO_ENV_VAR = "DOC2KB_POPO_REPO"
POPO_MODEL = "mineru"
POPO_ENTRY_POINTS = {
    "normalization": "post_processing/label_normalization.py",
    "inference": "post_processing/run_inference.py",
    "build_tree": "post_processing/get_json_tree.py",
}
# run_inference.py imports this one; checked so a partial checkout fails early.
POPO_REQUIRED_FILES = (*POPO_ENTRY_POINTS.values(), "post_processing/inference.py")
# What upstream's scripts/run_inference.sh exports (its default backend is an
# OpenAI-compatible client with an empty URL).
POPO_DEFAULT_ENV = {
    "POPO_INFERENCE_BACKEND": "transformers",
    "POPO_MAX_NEW_TOKENS": "8192",
}
MINERU_READER_FILES = ("_model.json", "_middle.json", "_content_list.json")
WORK_DIRNAME = "_popo"

SCRIPTS_DIR = Path(__file__).resolve().parent
# Sentinel written by bootstrap_popo.py holding the path to Popo's dedicated
# env python, which runs Popo's entry points.
POPO_PY_SENTINEL = ".doc2kb-popo-python"

POPO_NOT_FOUND_HINT = (
    "MinerU-Popo repo not configured. Either:\n"
    f"  1) Set the env var: export {POPO_ENV_VAR}=/abs/path/to/MinerU-Popo\n"
    "  2) Pass --popo-repo /abs/path/to/MinerU-Popo on the command line\n"
    "  3) Bootstrap it (clone + env + ~16 GB model): "
    "python3 <skill_dir>/scripts/ensure_env.py bootstrap_popo.py\n"
    "Manual install per upstream README:\n"
    "  git clone https://github.com/opendatalab/MinerU-Popo.git\n"
    "  cd MinerU-Popo\n"
    "  conda create -n popo python=3.10 && conda activate popo\n"
    "  pip install -r requirements.txt\n"
    "  hf download DreamEternal/MinerU-Popo --local-dir models/Mineru-Popo"
)


def _resolve_popo_repo(arg: str | None) -> Path | None:
    """Return the Popo repo path. Precedence: --popo-repo > $DOC2KB_POPO_REPO >
    the bootstrap_popo.py auto-default (<state-dir>/popo/MinerU-Popo) if it
    exists. We don't otherwise auto-detect — too easy to pick the wrong checkout."""
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


def _popo_python(popo_repo: Path) -> str | None:
    """The bootstrapped env's python (sentinel), else `python3` from PATH — a
    manual install's activated env."""
    sentinel = popo_repo / POPO_PY_SENTINEL
    if sentinel.is_file():
        try:
            py = sentinel.read_text(encoding="utf-8").strip()
        except OSError:
            py = ""
        if py and Path(py).is_file():
            return py
    return shutil.which("python3")


def _popo_subprocess_env(popo_repo: Path) -> dict:
    env = dict(os.environ)
    for key, value in POPO_DEFAULT_ENV.items():
        env.setdefault(key, value)
    # Popo's transformers backend reads the model from $POPO_MODEL_PATH
    # (upstream default "popo_model", a relative dir that won't exist).
    model_dir = popo_repo / "models" / "Mineru-Popo"
    if model_dir.is_dir():
        env.setdefault("POPO_MODEL_PATH", str(model_dir))
    if sys.platform == "darwin":
        # The 4B VLM runs on MPS; route ops MPS lacks to CPU instead of crashing.
        env.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    return env


def _validate_popo_layout(repo: Path) -> list[str]:
    return [rel for rel in POPO_REQUIRED_FILES if not (repo / rel).is_file()]


def _list_cached_docs(cache_dir: Path) -> list[str]:
    """Each subfolder of <kb_dir>/_mineru/ is one cached MinerU run named
    after the doc_id. Dotfiles are skipped."""
    if not cache_dir.is_dir():
        return []
    return sorted(
        p.name for p in cache_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def _cache_problem(cache_dir: Path, doc_id: str) -> str | None:
    """Why Popo can't take this cached doc, or None when it can."""
    doc_dir = cache_dir / doc_id
    vlm = doc_dir / "vlm"
    if not any((vlm / f"{doc_id}{suffix}").is_file() for suffix in MINERU_READER_FILES):
        return (f"no {vlm}/{doc_id}_content_list.json — cache predates MinerU 4; "
                "re-run extract_pdf_mineru.py --keep-raw")
    if not (doc_dir / f"{doc_id}.pdf").is_file():
        return (f"no source PDF {doc_dir}/{doc_id}.pdf (Popo renders its pages); "
                "re-run extract_pdf_mineru.py --keep-raw")
    return None


def _run_popo(label: str, python: str, script: Path, args: list[str],
              cwd: Path, env: dict) -> tuple[int, str]:
    """Run one Popo entry point, streaming its output to our stderr. Returns
    (exit code, last lines of output)."""
    log(f"{label}: {Path(python).name} {script.name}", prefix="popo")
    tail: collections.deque[str] = collections.deque(maxlen=15)
    proc = subprocess.Popen(
        [python, str(script), *args], cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            tail.append(line)
            print(f"[popo:{label}] {line}", file=sys.stderr, flush=True)
    return proc.wait(), "\n".join(tail)


def _select(src_dir: Path, doc_ids: list[str], dst_dir: Path) -> list[str]:
    """Fresh `dst_dir` with only `<id>.json` of `doc_ids` copied from `src_dir`
    (run_inference.py / get_json_tree.py take every *.json in a directory).
    Returns the ids that had a file."""
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    dst_dir.mkdir(parents=True)
    present = []
    for doc_id in doc_ids:
        src = src_dir / f"{doc_id}.json"
        if src.is_file():
            shutil.copyfile(src, dst_dir / src.name)
            present.append(doc_id)
    return present


def _unlink_outputs(out_dir: Path, doc_ids: list[str]) -> None:
    """Drop this run's previous outputs so a doc Popo silently skips can't
    pass for done on the strength of an old file."""
    for doc_id in doc_ids:
        (out_dir / f"{doc_id}.json").unlink(missing_ok=True)


def _collect_trees(tree_dir: Path, doc_ids: list[str], kb_docs_dir: Path) -> list[dict]:
    """Copy `<tree_dir>/<doc_id>.json` next to the kb doc as `.tree.json`."""
    summaries: list[dict] = []
    for doc_id in doc_ids:
        tree = tree_dir / f"{doc_id}.json"
        if not tree.is_file():
            summaries.append({"doc_id": doc_id, "tree_copied": False,
                              "reason": "popo produced no tree"})
            continue
        kb_doc = next(
            (p for p in sorted(kb_docs_dir.glob(f"{doc_id}-*.md")) if p.is_file()),
            None,
        )
        if kb_doc is None:
            summaries.append({"doc_id": doc_id, "tree_copied": False,
                              "reason": f"no kb doc matching {doc_id}-*.md",
                              "source": str(tree)})
            continue
        sidecar = kb_doc.with_suffix(".tree.json")
        try:
            shutil.copyfile(tree, sidecar)
        except OSError as e:
            summaries.append({"doc_id": doc_id, "tree_copied": False,
                              "reason": f"copy failed: {e}"})
            continue
        summaries.append({"doc_id": doc_id, "tree_copied": True,
                          "tree_path": str(sidecar), "source": str(tree)})
    return summaries


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Run MinerU-Popo over cached MinerU outputs in a doc2kb kb. "
            "Strictly opt-in — requires a MinerU-Popo checkout with its model."
        ),
    )
    ap.add_argument("kb_dir", help="Path to the doc2kb kb directory")
    ap.add_argument(
        "--popo-repo",
        default=None,
        help=f"Path to the MinerU-Popo clone. Falls back to ${POPO_ENV_VAR}, "
             "then the bootstrap_popo.py default.",
    )
    ap.add_argument(
        "--doc-id",
        default=None,
        help="Postprocess only this doc_id. Default: every doc under "
             "<kb_dir>/_mineru/.",
    )
    # Pre-4.2 the docs were staged into the shared Popo checkout under
    # post-process/mineru/<run-name>/; everything now lives in <kb_dir>/_popo/.
    ap.add_argument("--run-name", default=None, help=argparse.SUPPRESS)
    ap.add_argument(
        "--skip-normalization", action="store_true",
        help="Reuse <kb_dir>/_popo/label_normalization/ — e.g. re-running "
             "inference after editing the normalized labels.",
    )
    ap.add_argument(
        "--skip-inference", action="store_true",
        help="Reuse <kb_dir>/_popo/inference/ — re-build trees only.",
    )
    ap.add_argument(
        "--auto-setup", action="store_true",
        help="If no Popo repo is configured, run bootstrap_popo.py first "
             "(clone + env + ~16 GB model download), then proceed.",
    )
    args = ap.parse_args()
    warnings: list[str] = []
    if args.run_name is not None:
        warnings.append("--run-name ignored: Popo's working files now live in "
                        f"<kb_dir>/{WORK_DIRNAME}/")

    kb_dir = Path(args.kb_dir).expanduser().resolve()
    if not kb_dir.is_dir():
        emit_failure(f"kb_dir not found: {kb_dir}")
        return 1

    popo_repo = _resolve_popo_repo(args.popo_repo)
    if popo_repo is None and args.auto_setup:
        log("Popo repo not configured; --auto-setup → running bootstrap_popo.py "
            "(this may clone the repo, build an env, and download ~16 GB)",
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
            "Popo repo missing required files: " + ", ".join(missing)
            + "\n\nIs this the right checkout? Expected layout per Popo README.\n\n"
            + POPO_NOT_FOUND_HINT
        )
        return 2
    python = _popo_python(popo_repo)
    if python is None:
        emit_failure(
            f"no Python for Popo: {popo_repo / POPO_PY_SENTINEL} is missing and "
            "python3 is not on PATH. Activate Popo's env or run bootstrap_popo.py.")
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
            emit_failure(f"doc_id {args.doc_id!r} not in cache. Available: {available}")
            return 1
        targets = [args.doc_id]
    else:
        targets = available

    failed: dict[str, str] = {}
    for doc_id in targets:
        problem = _cache_problem(cache_dir, doc_id)
        if problem:
            failed[doc_id] = problem
    ready = [d for d in targets if d not in failed]
    if not ready:
        emit_failure("no cached doc is usable by Popo:\n" + "\n".join(
            f"  {d}: {r}" for d, r in failed.items()))
        return 1

    work = kb_dir / WORK_DIRNAME
    label_dir = work / "label_normalization"
    label_model_dir = label_dir / POPO_MODEL   # label_normalization.py appends <model>/
    infer_dir = work / "inference"
    tree_dir = work / "build_tree"
    txt_dir = work / "build_tree_txt"
    select_root = work / ".select"
    for d in (label_model_dir, infer_dir, tree_dir, txt_dir):
        d.mkdir(parents=True, exist_ok=True)

    env = _popo_subprocess_env(popo_repo)
    post = popo_repo / "post_processing"
    log(f"{len(ready)} doc(s) → Popo at {popo_repo} (work dir {work})", prefix="popo")

    def drop(doc_ids: list[str], present: list[str], reason: str) -> list[str]:
        for d in doc_ids:
            if d not in present:
                failed[d] = reason
        return [d for d in doc_ids if d in present]

    def step_failed(label: str, rc: int, tail: str) -> int:
        emit_failure(f"popo {label} failed (exit {rc}):\n{tail or 'no output captured'}",
                     extra={"work_dir": str(work)})
        return 1

    # 1. Label normalization: MinerU cache → Popo's per-page block format.
    if args.skip_normalization:
        log("skipping normalization (--skip-normalization)", prefix="popo")
    else:
        pdf_map = work / "pdf_map.json"
        pdf_map.write_text(json.dumps(
            {d: str(cache_dir / d / f"{d}.pdf") for d in ready},
            ensure_ascii=False, indent=2), encoding="utf-8")
        _unlink_outputs(label_model_dir, ready)
        rc, tail = _run_popo("normalization", python, post / "label_normalization.py", [
            "--model", POPO_MODEL, "--input-dir", str(cache_dir),
            "--doc-id", *ready, "--output-dir", str(label_dir),
            "--pdf-map-json", str(pdf_map),
        ], popo_repo, env)
        if rc != 0:
            return step_failed("normalization", rc, tail)
    present = [d for d in ready if (label_model_dir / f"{d}.json").is_file()]
    ready = drop(ready, present, "no normalized labels (Popo's MineruReader skipped "
                 "the doc, or --skip-normalization without an earlier run)")

    # 2. Inference: the 4B VLM labels truncation, title levels, image links.
    if ready and args.skip_inference:
        log("skipping inference (--skip-inference)", prefix="popo")
    elif ready:
        selected = _select(label_model_dir, ready, select_root / "labels")
        _unlink_outputs(infer_dir, selected)
        rc, tail = _run_popo("inference", python, post / "run_inference.py", [
            "--model", POPO_MODEL, "--input-dir", str(select_root / "labels"),
            "--output-dir", str(infer_dir), "--raw-output-root", "", "--limit", "0",
        ], popo_repo, env)
        if rc != 0:
            return step_failed("inference", rc, tail)
    present = [d for d in ready if (infer_dir / f"{d}.json").is_file()]
    ready = drop(ready, present, "no inference output (or --skip-inference "
                 "without an earlier run)")

    # 3. Tree building (no model).
    if ready:
        selected = _select(infer_dir, ready, select_root / "inference")
        _unlink_outputs(tree_dir, selected)
        rc, tail = _run_popo("build_tree", python, post / "get_json_tree.py", [
            "--input-dir", str(select_root / "inference"),
            "--output-dir", str(tree_dir), "--txt-dir", str(txt_dir),
        ], popo_repo, env)
        if rc != 0:
            return step_failed("build_tree", rc, tail)
    shutil.rmtree(select_root, ignore_errors=True)

    summaries = _collect_trees(tree_dir, ready, kb_dir / "docs")
    summaries += [{"doc_id": d, "tree_copied": False, "reason": r}
                  for d, r in failed.items()]
    summaries.sort(key=lambda s: s["doc_id"])

    success_extras = {
        "popo_repo": str(popo_repo),
        "work_dir": str(work),
        "processed": targets,
        "results": summaries,
    }
    log_path = kb_dir / "_logs" / "popo.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(success_extras, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    if not any(s["tree_copied"] for s in summaries):
        emit_failure("popo produced no tree:\n" + "\n".join(
            f"  {s['doc_id']}: {s['reason']}" for s in summaries), extra=success_extras)
        return 1
    emit_success(log_path, "", warnings=warnings, extra=success_extras)
    return 0


if __name__ == "__main__":
    sys.exit(main())
