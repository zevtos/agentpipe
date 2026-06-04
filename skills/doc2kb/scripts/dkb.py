#!/usr/bin/env python3
"""
dkb.py — standalone one-shot CLI for the doc2kb pipeline.

This is the "no-Claude" entry point. It runs the same Phase 2→5 machinery an
agent would drive (scout → decide → extract → assemble), but with the decision
step automated by a fixed policy so a human can build a knowledge base from a
folder with one command:

    dkb <input_dir> <output_kb_dir> [options]

It is a *thin orchestrator*: it never re-implements extraction or scoring. It
shells out to the existing scripts through `ensure_env.py` (so the venv is
bootstrapped exactly as documented in SKILL.md) and reuses `apply_overrides.py`
to resolve scout decisions deterministically — it never hand-edits _scout.json.

Pure stdlib, runs under the *system* python3 (not the venv). Each pipeline phase
is a subprocess: `python3 ensure_env.py <phase_script> ...`. ensure_env handles
venv bootstrap on the first phase that touches it.

Heavy-deps stay opt-in (the project invariant): MinerU/Popo are only reached via
the explicit `--tier mineru` / `--enable-mineru` / `--always-popo` flags. Nothing
heavy is ever installed or routed silently.

Subcommands:
    run       (default) full pipeline: scout → auto-decide → extract → manifest
    scout     Phase 2 only — classify the corpus, write _scout.json
    extract   Phase 4 only — extract from an existing _scout.json
    manifest  Phase 5 only — (re)assemble manifest/INDEX/llms.txt/AGENTS.md
    install   drop a `dkb` launcher onto your PATH (so you can type `dkb …`)

`dkb <in> <out>` with two positional paths is shorthand for `dkb run <in> <out>`.

Decision policy (run / extract auto-resolve):
    --decide skip      (default) every file scout flagged for a decision
                       (scanned/encrypted/corrupt/unsupported/huge) is skipped.
    --decide proceed   huge files extract anyway with their normal extractor;
                       everything else still skips (encrypted needs a password,
                       scanned needs OCR/VLM, etc. — out of scope for the basic
                       non-interactive flow).

Exit codes:
    0  pipeline finished, no extraction errors
    2  setup/usage failure (bad args, a phase refused to start)
    3  at least one file landed in the extractor's error bucket
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent
ENSURE_ENV = SCRIPTS_DIR / "ensure_env.py"

# Auto-decide writes its own overrides file so it never clobbers a hand-authored
# <kb_dir>/_overrides.json (apply_overrides' default config path).
DKB_OVERRIDES_NAME = "_overrides.dkb.json"


def _info(msg: str) -> None:
    print(f"[dkb] {msg}", file=sys.stderr, flush=True)


def _fail(msg: str) -> int:
    print(f"[dkb] ERROR: {msg}", file=sys.stderr, flush=True)
    return 2


def _ensure_env_cmd(tier: Optional[str], script: str, *args: str) -> list[str]:
    """Build a `python3 ensure_env.py [--tier T] <script> args...` command.

    Uses the *system* interpreter to launch ensure_env (it is stdlib-only and
    must run before the venv exists); ensure_env then execs the venv python for
    the target script.
    """
    cmd = [sys.executable, str(ENSURE_ENV)]
    if tier:
        cmd += ["--tier", tier]
    cmd += [script, *args]
    return cmd


def _run_phase(cmd: list[str], *, capture: bool = False) -> tuple[int, str]:
    """Run a pipeline phase. stderr always streams to the terminal (progress).

    capture=True pipes stdout back (for phases whose machine output we parse:
    extract_corpus' summary, apply_overrides' JSON). capture=False lets stdout
    stream too (scout's human summary, build_manifest's report).
    """
    if capture:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
        return proc.returncode, proc.stdout or ""
    proc = subprocess.run(cmd)
    return proc.returncode, ""


def _last_json_line(stdout: str) -> Optional[dict]:
    """Extract the last JSON object printed on stdout (the summary line)."""
    for line in reversed([ln for ln in stdout.splitlines() if ln.strip()]):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


# ---------- auto-decide (Phase 3, automated) ----------

def _auto_resolve(kb_root: Path, tier: Optional[str], decide: str) -> Optional[int]:
    """Clear every file's `action_required` per the `decide` policy.

    Reads _scout.json, builds a dkb-owned _overrides.dkb.json keyed by doc-id,
    and applies it via apply_overrides.py (validated + atomic). Returns an exit
    code on failure, or None on success / nothing to do.
    """
    scout_path = kb_root / "_scout.json"
    if not scout_path.is_file():
        return _fail(f"no _scout.json in {kb_root} — run `dkb scout` (or `dkb run`) first")
    try:
        scout = json.loads(scout_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return _fail(f"_scout.json unreadable: {e}")

    pending = [f for f in (scout.get("files") or []) if f.get("action_required")]
    if not pending:
        return None

    overrides = []
    skipped, proceeded = 0, 0
    for f in pending:
        doc_id = f.get("id")
        ar = f.get("action_required")
        strat = f.get("extraction_strategy")
        if not isinstance(doc_id, str):
            continue
        # "proceed" only has a safe automatic meaning for size-gated files that
        # already carry a runnable extractor. Everything else needs human input
        # (password / OCR backend), so the basic flow skips it.
        if decide == "proceed" and ar == "ask_user_proceed_huge" and strat:
            overrides.append({"match": doc_id, "strategy": strat,
                              "note": "dkb --decide proceed (huge file)"})
            proceeded += 1
        else:
            overrides.append({"match": doc_id, "strategy": "skip",
                              "note": f"dkb --decide {decide} (was {ar})"})
            skipped += 1

    cfg_path = kb_root / DKB_OVERRIDES_NAME
    cfg_path.write_text(
        json.dumps({"version": 1, "overrides": overrides},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    _info(f"auto-decide ({decide}): {len(pending)} flagged file(s) → "
          f"{proceeded} proceed, {skipped} skip")
    rc, out = _run_phase(
        _ensure_env_cmd(tier, "apply_overrides.py", str(kb_root),
                        "--config", str(cfg_path)),
        capture=True,
    )
    result = _last_json_line(out)
    if rc != 0 or not (result and result.get("ok")):
        reason = (result or {}).get("reason", f"exit {rc}")
        return _fail(f"auto-decide failed: {reason}")
    return None


# ---------- phases ----------

def _scout(kb_root: Path, input_dir: Path, tier: Optional[str],
           enable_mineru: bool) -> int:
    args = [str(input_dir), str(kb_root)]
    if enable_mineru:
        args.append("--enable-mineru")
    rc, _ = _run_phase(_ensure_env_cmd(tier, "scout_corpus.py", *args))
    return rc


def _extract(kb_root: Path, tier: Optional[str], *, timeout: Optional[int],
             normalize: bool, always_popo: bool, quiet: bool) -> tuple[int, Optional[dict]]:
    args = [str(kb_root)]
    if timeout is not None:
        args += ["--timeout", str(timeout)]
    if normalize:
        args.append("--normalize")
    if quiet:
        args.append("--quiet")
    env_note = ""
    if always_popo:
        os.environ["DOC2KB_ALWAYS_POPO"] = "1"
        env_note = " (DOC2KB_ALWAYS_POPO=1)"
    _info(f"extract{env_note}")
    rc, out = _run_phase(_ensure_env_cmd(tier, "extract_corpus.py", *args),
                         capture=True)
    summary = _last_json_line(out)
    return rc, summary


def _manifest(kb_root: Path, tier: Optional[str]) -> int:
    rc, _ = _run_phase(_ensure_env_cmd(tier, "build_manifest.py", str(kb_root)))
    return rc


def _finish_extract(kb_root: Path, rc: int, summary: Optional[dict]) -> int:
    """Map an extract phase result to a dkb exit code + printed report.

    extract_corpus exits 2 only when it refuses to start (missing _scout.json
    or leftover action_required). In the dkb flow auto-decide clears the gate
    first, so this is defensive — but a refusal must surface as a failure, not
    a silent success.
    """
    if rc == 2:
        reason = (summary or {}).get("reason", "extract refused to start")
        return _fail(reason)
    _report(kb_root, summary)
    counts = (summary or {}).get("counts") or {}
    return 3 if counts.get("error", 0) > 0 else 0


def _report(kb_root: Path, summary: Optional[dict]) -> None:
    if not summary:
        _info("no extract summary to report (see output above)")
        return
    counts = summary.get("counts") or {}
    print()
    print(f"  dkb done → {kb_root}")
    print(f"    extracted={counts.get('extracted', 0)} "
          f"unchanged={counts.get('unchanged', 0)} "
          f"skipped={counts.get('skipped_by_decision', 0)} "
          f"errors={counts.get('error', 0)} "
          f"needs_attention={counts.get('needs_attention', 0)}")
    na = summary.get("needs_attention") or []
    for item in na:
        reason = item.get("reason", "?")
        path = item.get("source_path", item.get("id", "?"))
        hint = item.get("install_hint") or item.get("warning") or ""
        line = f"    ! {reason}: {path}"
        if hint:
            line += f" — {hint}"
        print(line)
    if summary.get("errors_log"):
        print(f"    errors logged → {summary['errors_log']}")
    print(f"    index → {kb_root / 'INDEX.md'}")


# ---------- launcher install ----------

def _shim_text() -> str:
    return (
        "#!/usr/bin/env bash\n"
        "# doc2kb standalone CLI launcher — generated by `dkb install`.\n"
        "# Override the skill location with DOC2KB_SKILL if you move the install.\n"
        "set -euo pipefail\n"
        f'SKILL="${{DOC2KB_SKILL:-{SKILL_DIR}}}"\n'
        'exec python3 "$SKILL/scripts/dkb.py" "$@"\n'
    )


def _cmd_text() -> str:
    return (
        "@echo off\r\n"
        "REM doc2kb standalone CLI launcher — generated by `dkb install`.\r\n"
        f'set "SKILL=%DOC2KB_SKILL%"\r\n'
        f'if "%SKILL%"=="" set "SKILL={SKILL_DIR}"\r\n'
        'python3 "%SKILL%\\scripts\\dkb.py" %*\r\n'
    )


def _on_path(bin_dir: Path) -> bool:
    parts = os.environ.get("PATH", "").split(os.pathsep)
    resolved = bin_dir.resolve()
    for p in parts:
        if p and Path(p).expanduser().resolve(strict=False) == resolved:
            return True
    return False


def _install(bin_dir: Optional[str], force: bool) -> int:
    is_win = os.name == "nt"
    default_bin = (Path.home() / "bin") if is_win else (Path.home() / ".local" / "bin")
    target_dir = Path(bin_dir).expanduser() if bin_dir else default_bin
    shim = target_dir / ("dkb.cmd" if is_win else "dkb")

    if shim.exists() and not force:
        return _fail(f"{shim} already exists (pass --force to overwrite)")

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        shim.write_text(_cmd_text() if is_win else _shim_text(), encoding="utf-8")
        if not is_win:
            shim.chmod(0o755)
    except OSError as e:
        return _fail(f"could not write launcher: {e}")

    _info(f"installed launcher → {shim}")
    _info(f"  it runs: python3 {SCRIPTS_DIR / 'dkb.py'}")
    if not _on_path(target_dir):
        _info(f"{target_dir} is not on your PATH. Add it, e.g.:")
        _info(f'  echo \'export PATH="{target_dir}:$PATH"\' >> ~/.zshrc && '
              "exec $SHELL")
    else:
        _info("you can now run `dkb <input_dir> <output_kb_dir>`")
    return 0


# ---------- run pipeline ----------

def _run_pipeline(args: argparse.Namespace) -> int:
    input_dir = Path(args.input_dir).expanduser().resolve()
    kb_root = Path(args.output_dir).expanduser().resolve()
    if not input_dir.is_dir():
        return _fail(f"input_dir is not a directory: {input_dir}")
    tier = "mineru" if args.tier == "mineru" else None
    if args.enable_mineru and not tier:
        _info("--enable-mineru routes image-only PDFs to MinerU; without "
              "`--tier mineru` they will surface as needs_install in the report.")

    _info(f"scout {input_dir} → {kb_root}")
    if _scout(kb_root, input_dir, tier, args.enable_mineru) != 0:
        return _fail("scout failed (see output above)")

    resolved = _auto_resolve(kb_root, tier, args.decide)
    if resolved is not None:
        return resolved

    rc, summary = _extract(kb_root, tier, timeout=args.timeout,
                           normalize=args.normalize, always_popo=args.always_popo,
                           quiet=args.quiet)
    if rc == 2:
        return _fail((summary or {}).get("reason", "extract refused to start"))

    if _manifest(kb_root, tier) != 0:
        _info("WARNING: build_manifest failed — KB docs/ are extracted but the "
              "index may be stale")

    return _finish_extract(kb_root, rc, summary)


# ---------- argument parsing ----------

def _add_pipeline_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--decide", choices=["skip", "proceed"], default="skip",
                   help="how to auto-resolve scout decisions (default: skip)")
    p.add_argument("--enable-mineru", action="store_true",
                   help="route image-only PDFs through MinerU (needs --tier mineru)")
    p.add_argument("--tier", choices=["mineru"], default=None,
                   help="install the opt-in heavy tier before running")
    p.add_argument("--always-popo", action="store_true",
                   help="run MinerU→Popo stage 2 on every mineru doc (opt-in, heavy)")
    p.add_argument("--normalize", action="store_true",
                   help="run normalize_md after each extraction")
    p.add_argument("--timeout", type=int, default=None,
                   help="per-file extractor timeout in seconds")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="suppress per-file extract progress")


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="dkb",
        description="Standalone doc2kb pipeline: a folder of mixed documents → "
                    "an LLM-ready knowledge base, in one command.",
    )
    sub = ap.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="full pipeline (scout→extract→manifest)")
    p_run.add_argument("input_dir", help="corpus root")
    p_run.add_argument("output_dir", help="kb output directory (created)")
    _add_pipeline_flags(p_run)

    p_scout = sub.add_parser("scout", help="Phase 2 only — classify the corpus")
    p_scout.add_argument("input_dir")
    p_scout.add_argument("output_dir")
    p_scout.add_argument("--enable-mineru", action="store_true")
    p_scout.add_argument("--tier", choices=["mineru"], default=None)

    p_ext = sub.add_parser("extract", help="Phase 4 only — extract from _scout.json")
    p_ext.add_argument("output_dir", help="kb directory with _scout.json")
    p_ext.add_argument("--decide", choices=["skip", "proceed"], default="skip")
    p_ext.add_argument("--tier", choices=["mineru"], default=None)
    p_ext.add_argument("--always-popo", action="store_true")
    p_ext.add_argument("--normalize", action="store_true")
    p_ext.add_argument("--timeout", type=int, default=None)
    p_ext.add_argument("-q", "--quiet", action="store_true")

    p_man = sub.add_parser("manifest", help="Phase 5 only — (re)assemble the index")
    p_man.add_argument("output_dir", help="kb directory")
    p_man.add_argument("--tier", choices=["mineru"], default=None)

    p_inst = sub.add_parser("install", help="install a `dkb` launcher on your PATH")
    p_inst.add_argument("--bin-dir", default=None,
                        help="target directory (default ~/.local/bin)")
    p_inst.add_argument("--force", action="store_true",
                        help="overwrite an existing launcher")

    # Shorthand: `dkb <in> <out> [flags]` == `dkb run <in> <out> [flags]`.
    # Detected when the first token is neither a known subcommand nor a flag.
    argv = sys.argv[1:]
    known = {"run", "scout", "extract", "manifest", "install"}
    if argv and argv[0] not in known and not argv[0].startswith("-"):
        argv = ["run", *argv]

    args = ap.parse_args(argv)
    if not args.command:
        ap.print_help()
        return 2

    if args.command == "run":
        return _run_pipeline(args)
    if args.command == "install":
        return _install(args.bin_dir, args.force)

    tier = "mineru" if getattr(args, "tier", None) == "mineru" else None
    if args.command == "scout":
        input_dir = Path(args.input_dir).expanduser().resolve()
        kb_root = Path(args.output_dir).expanduser().resolve()
        if not input_dir.is_dir():
            return _fail(f"input_dir is not a directory: {input_dir}")
        return _scout(kb_root, input_dir, tier, args.enable_mineru)
    if args.command == "extract":
        kb_root = Path(args.output_dir).expanduser().resolve()
        resolved = _auto_resolve(kb_root, tier, args.decide)
        if resolved is not None:
            return resolved
        rc, summary = _extract(kb_root, tier, timeout=args.timeout,
                               normalize=args.normalize,
                               always_popo=args.always_popo, quiet=args.quiet)
        return _finish_extract(kb_root, rc, summary)
    if args.command == "manifest":
        kb_root = Path(args.output_dir).expanduser().resolve()
        return _manifest(kb_root, tier)
    return 2


if __name__ == "__main__":
    sys.exit(main())
