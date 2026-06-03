#!/usr/bin/env python3
"""
extract_corpus.py — Phase 4 deterministic batch dispatcher for doc2kb.

CLI:
    extract_corpus.py <kb_dir> [--timeout 600] [--normalize] [--quiet]

Effect:
    Reads <kb_dir>/_scout.json and runs the entire mechanical Phase-4 loop in
    Python: for each runnable file it dispatches the scout-assigned
    `extraction_strategy` to the right extractor through `ensure_env.py`, writes
    `docs/<id>-<slug>.md`, accumulates `_logs/errors.json`, and prints ONE JSON
    summary (with a `needs_attention[]` queue) as its last stdout line. This
    replaces the per-file command-construction + JSON-parsing the orchestrating
    agent used to do by hand, collapsing O(N) agent tool-calls into one.

Scope — what this dispatcher OWNS vs. what stays the agent's:
    OWNS (deterministic mechanical dispatch): strategy→script mapping, the
    ensure_env invocation, last-line-JSON parsing, error logging, idempotent
    re-runs (skip when the produced doc's `source_sha256` already matches
    scout's `sha256`).

    SURFACES but never resolves (the agent decides — see SKILL.md / pitfalls):
      - `visual_transcription`     — an `ok:true` PDF whose warnings contain
        `mangled_visual_layout`: the body exists but positioned math needs a
        manual Read-tool transcription.
      - `dropped_pictures_residual`— an `ok:true` PDF with residual
        `dropped_pictures`: the body exists but some figures/formulas were
        dropped. `pages` is recovered by re-scanning the produced doc for
        `==> picture … intentionally omitted <==` placeholders (the warning
        string itself carries only counts, not a page list).
      - `needs_install`            — an extractor exited 2 (`.doc` with no
        system converter, or `mineru` CLI absent): NOT an error, NOT corrupt.
    These land in `needs_attention[]`; the agent runs the mineru page-patch /
    manual transcription / converter install afterward, then build_manifest.

    Does NOT: ask the Phase-3 user question, run mineru patching or
    normalize-by-default, run build_manifest, re-run scout, or trigger any
    heavy/opt-in install. For `mineru` strategy it invokes the extractor
    WITHOUT `--tier mineru` — if the opt-in tier isn't installed the extractor
    exits 2 and the file surfaces as `needs_install`, so the driver never
    silently auto-installs the ~3 GB ML tier (doc2kb heavy-deps-opt-in rule).

Per-file MinerU settings + env-gated Popo (both default-off):
    - `_scout.files[].mineru` (a structured `{backend,lang,keep_raw}` block,
      stamped by apply_overrides.py) is rendered into whitelisted CLI flags for
      the mineru extractor — never raw arg strings (scout is agent-mutable).
    - DOC2KB_ALWAYS_POPO (env) routes every mineru-extracted doc through Popo as
      a non-fatal stage-2 pass; a per-file `popo` bool overrides it. Routed docs
      get `--keep-raw` forced so Popo finds the `<kb>/_mineru/<id>/` cache. With
      DOC2KB_POPO_AUTO set, Popo self-bootstraps (clone+env+model). With neither
      env nor any per-file popo flag, NOTHING extra runs — behavior is unchanged.

Refuse-to-start (exit 2, nothing extracted):
    - `_scout.json` missing or unparseable.
    - Any file still carries a non-null `action_required` (Phase 3 not done).
      The agent must resolve every decision in `_scout.json` first: set
      `extraction_strategy` to the chosen outcome AND null out `action_required`.

Exit codes:
    0 — all files reached a terminal state, no hard errors. `needs_attention`
        (both flavours) is NOT a failure.
    2 — refused to start (see above).
    3 — ran to completion but ≥1 file landed in the `error` bucket.

Invariant: every `_scout.files[]` entry lands in exactly one of
    {extracted, unchanged, skipped_by_decision, error, needs_install}.
    `visual_transcription` / `dropped_pictures_residual` files are counted in
    `extracted` AND listed in `needs_attention[]` with `extracted_but_flagged:
    true`; `counts.needs_attention` counts only `needs_install` entries, so the
    buckets never double-count.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from _common import (  # noqa: E402
    classify_pdf_warning,
    kb_doc_filename,
    log as _common_log,
    read_body,
    read_frontmatter,
    residual_picture_pages,
    tool_version_string,
    validate_source_rel,
)
from extract_pdf_mineru import SUPPORTED_BACKENDS  # noqa: E402  (constant only)


# Canonical scout id shape (`doc-001`, `doc-042`, …). The driver re-asserts it
# because _scout.json is derived from an untrusted corpus and is agent-mutable
# — a hostile id with path separators is a write-primitive (CWE-22/73).
_DOC_ID_RE = re.compile(r"doc-\d{3,}")


SCRIPTS_DIR = Path(__file__).resolve().parent
ENSURE_ENV = SCRIPTS_DIR / "ensure_env.py"
DEFAULT_TIMEOUT_SEC = 600

# strategy → (script, extra CLI args). Mirrors references/extraction-recipes.md.
# Asset flags are intentionally omitted: every image-aware extractor already
# defaults assets to <out>.parent.parent/assets == <kb_dir>/assets.
DISPATCH: dict[str, tuple[str, list[str]]] = {
    "pymupdf4llm":     ("extract_pdf_pymupdf4llm.py", []),
    "mineru":          ("extract_pdf_mineru.py", []),
    "mammoth":         ("extract_docx.py", []),
    "doc":             ("extract_doc.py", []),
    "rtf":             ("extract_rtf.py", []),
    "python-pptx":     ("extract_pptx.py", []),
    "passthrough-md":  ("extract_md_txt.py", ["--mode", "md"]),
    "passthrough-txt": ("extract_md_txt.py", ["--mode", "txt"]),
    "trafilatura":     ("extract_html.py", []),
    "ipynb":           ("extract_ipynb.py", []),
}

# Explicit "do not extract" outcomes already decided upstream (scout or Phase 3).
NON_RUNNABLE = {"skip", "not_in_mvp", "needs_ocr_or_vlm", "needs_password"}

_LANG_RE = re.compile(r"^[a-z]{2,}$")
# Popo (and its optional auto-bootstrap) are heavy: VLM inference on a 4B model
# and, when DOC2KB_POPO_AUTO is set, a multi-GB model download. Give them a
# generous ceiling independent of the per-file extractor --timeout.
POPO_TIMEOUT_SEC = 6 * 3600


def log(msg: str) -> None:
    _common_log(msg, prefix="doc2kb extract")


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _mineru_flags(file_entry: dict, force_keep_raw: bool) -> tuple[list[str], list[str]]:
    """Render safe per-file MinerU CLI flags from the structured `mineru` block
    that apply_overrides stamped onto the scout entry. Whitelisted values only —
    never raw arg strings (scout is agent-mutable / untrusted-corpus-derived).
    Returns (flags, warnings)."""
    flags: list[str] = []
    warns: list[str] = []
    block = file_entry.get("mineru")
    if isinstance(block, dict):
        backend = block.get("backend")
        if backend in SUPPORTED_BACKENDS:
            flags += ["--backend", backend]
        elif backend is not None:
            warns.append(f"ignored invalid mineru.backend {backend!r}")
        lang = block.get("lang")
        if isinstance(lang, str) and _LANG_RE.match(lang):
            flags += ["--lang", lang]
        elif lang is not None:
            warns.append(f"ignored invalid mineru.lang {lang!r}")
        if block.get("keep_raw") is True:
            force_keep_raw = True
    if force_keep_raw and "--keep-raw" not in flags:
        flags.append("--keep-raw")
    return flags, warns


def _emit(obj: dict) -> None:
    """Print the single canonical JSON summary line to stdout."""
    print(json.dumps(obj, ensure_ascii=False))


def _parse_last_json_line(stdout: str) -> dict | None:
    """Return the last stdout line that parses as a JSON object, or None.
    Extractors print exactly one contract JSON line; scanning from the end
    tolerates stray trailing output without misreading it."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _run_extractor(strategy: str, in_path: Path, out_path: Path,
                   doc_id: str, source_rel: str, timeout: int,
                   extra_flags: list[str] | None = None):
    """Invoke one extractor through ensure_env.py. Returns
    (returncode_or_'timeout', parsed_json_or_None, stderr_tail)."""
    script, extra = DISPATCH[strategy]
    argv = [
        sys.executable, str(ENSURE_ENV), script,
        str(in_path), str(out_path),
        "--doc-id", doc_id, "--source-rel", source_rel, *extra,
        *(extra_flags or []),
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout", None, f"timeout after {timeout}s"
    return proc.returncode, _parse_last_json_line(proc.stdout or ""), (proc.stderr or "").strip()


def _invoke_popo(kb_root: Path, doc_id: str, auto_setup: bool):
    """Run postprocess_popo.py for one mineru-extracted doc through ensure_env.py.
    Returns (returncode_or_'timeout', parsed_json_or_None, stderr_tail)."""
    argv = [sys.executable, str(ENSURE_ENV), "postprocess_popo.py",
            str(kb_root), "--doc-id", doc_id]
    if auto_setup:
        argv.append("--auto-setup")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=POPO_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return "timeout", None, f"timeout after {POPO_TIMEOUT_SEC}s"
    return proc.returncode, _parse_last_json_line(proc.stdout or ""), (proc.stderr or "").strip()


def _run_popo(kb_root: Path, doc_ids: list[str],
              needs_attention: list[dict], quiet: bool) -> list[dict]:
    """Post-extraction Popo stage for every mineru doc routed through it. Strictly
    non-fatal: a Popo failure never flips the file's already-`extracted` state.
    Popo-not-configured (exit 2) surfaces in needs_attention[] (not counted in the
    file buckets, mirroring extracted_but_flagged). With DOC2KB_POPO_AUTO set,
    postprocess_popo self-bootstraps via --auto-setup before running."""
    auto = _truthy(os.environ.get("DOC2KB_POPO_AUTO"))
    results: list[dict] = []
    for doc_id in doc_ids:
        if not quiet:
            log(f"popo {doc_id}" + (" (auto-setup)" if auto else ""))
        rc, parsed, stderr_tail = _invoke_popo(kb_root, doc_id, auto_setup=auto)
        if rc == 2:
            hint = (parsed or {}).get("reason") or stderr_tail or "Popo not configured"
            needs_attention.append({
                "id": doc_id, "stage": "popo", "reason": "needs_install",
                "doc": None, "extracted_but_flagged": False, "install_hint": hint,
            })
            results.append({"doc_id": doc_id, "ok": False, "reason": "popo_not_configured"})
        elif rc == 0 and isinstance(parsed, dict) and parsed.get("ok") is True:
            results.append({"doc_id": doc_id, "ok": True,
                            "results": parsed.get("results")})
        else:
            reason = ((parsed or {}).get("reason") if isinstance(parsed, dict) else None) \
                or stderr_tail or f"exit {rc}"
            results.append({"doc_id": doc_id, "ok": False, "reason": reason})
            if not quiet:
                log(f"popo {doc_id} failed (non-fatal): {reason}")
    return results


def _normalize(out_path: Path, timeout: int) -> None:
    """Opt-in post-pass. Non-fatal: a normalize failure never flips the file
    to `error` (the extraction already succeeded)."""
    argv = [sys.executable, str(ENSURE_ENV), "normalize_md.py", str(out_path), "--write"]
    try:
        subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal
        log(f"normalize failed for {out_path.name}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic Phase-4 batch dispatcher: extract every "
                    "non-skipped file in _scout.json, surface judgment files."
    )
    ap.add_argument("kb_dir", help="kb directory containing _scout.json")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC,
                    help=f"per-file extractor timeout in seconds (default {DEFAULT_TIMEOUT_SEC})")
    ap.add_argument("--normalize", action="store_true",
                    help="run normalize_md.py --write after each successful extraction (default off)")
    ap.add_argument("--quiet", action="store_true", help="suppress per-file stderr progress")
    args = ap.parse_args()

    kb_root = Path(args.kb_dir).expanduser().resolve()
    scout_path = kb_root / "_scout.json"
    driver = f"doc2kb-extract-corpus@{tool_version_string()}"

    if not scout_path.is_file():
        _emit({"ok": False, "kb_root": str(kb_root),
               "reason": f"_scout.json not found at {scout_path} — run Phase 2 (scout) first",
               "driver": driver})
        return 2
    try:
        scout = json.loads(scout_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _emit({"ok": False, "kb_root": str(kb_root),
               "reason": f"_scout.json unreadable: {e}", "driver": driver})
        return 2

    files = scout.get("files") or []
    input_root = Path(scout.get("input_root", "")).expanduser().resolve()

    pending = [f for f in files if f.get("action_required")]
    if pending:
        _emit({"ok": False, "kb_root": str(kb_root),
               "reason": "unresolved action_required — run Phase 3 (Decide) and "
                         "clear each file's action_required before extracting",
               "files": [{"source_path": f.get("source_path"),
                          "action_required": f.get("action_required")} for f in pending],
               "driver": driver})
        return 2

    docs_dir = (kb_root / "docs").resolve()
    logs_dir = kb_root / "_logs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    counts = {"extracted": 0, "unchanged": 0, "skipped_by_decision": 0,
              "error": 0, "needs_attention": 0}
    needs_attention: list[dict] = []
    unclassified: list[dict] = []
    errors: list[dict] = []
    extracted_but_flagged = 0

    # Env-gated MinerU→Popo auto-route (default OFF — preserves the heavy-deps-
    # opt-in rule). DOC2KB_ALWAYS_POPO routes every mineru-extracted doc through
    # Popo; a per-file `popo` bool (set via apply_overrides) overrides it.
    popo_default = _truthy(os.environ.get("DOC2KB_ALWAYS_POPO"))
    popo_targets: list[str] = []

    t0 = time.time()
    for f in files:
        raw_id = f.get("id")
        raw_rel = f.get("source_path")
        strategy = f.get("extraction_strategy")
        # Stable label for error rows even when the fields are malformed.
        label = raw_rel if isinstance(raw_rel, str) else str(raw_rel)
        try:
            # --- Trust boundary: re-validate scout-controlled fields before
            # using them to build filesystem paths. _scout.json is derived
            # from an untrusted corpus and is agent-mutable, so a hostile
            # source_path (../, absolute) or id (path separators) must not
            # escape input_root / docs/. (CWE-22/73.)
            if not isinstance(raw_id, str) or not _DOC_ID_RE.fullmatch(raw_id):
                counts["error"] += 1
                errors.append({"source_path": label, "error": f"invalid scout id {raw_id!r}"})
                continue
            try:
                source_rel = validate_source_rel(raw_rel) if isinstance(raw_rel, str) else ""
            except ValueError:
                source_rel = ""
            if not source_rel:
                counts["error"] += 1
                errors.append({"source_path": label, "error": "invalid or escaping source_path"})
                continue
            doc_id = raw_id

            if strategy in NON_RUNNABLE:
                counts["skipped_by_decision"] += 1
                if not args.quiet:
                    log(f"skip {source_rel} (strategy={strategy})")
                continue
            if strategy not in DISPATCH:
                # Unknown/typo'd strategy — surface it, never silently skip.
                counts["error"] += 1
                errors.append({"source_path": source_rel,
                               "error": f"unrecognized extraction_strategy {strategy!r}"})
                if not args.quiet:
                    log(f"error {source_rel}: unrecognized strategy {strategy!r}")
                continue

            # Build paths from the validated fields and pin them inside the kb.
            out_path = (docs_dir / kb_doc_filename(doc_id, source_rel)).resolve()
            if out_path.parent != docs_dir:
                counts["error"] += 1
                errors.append({"source_path": source_rel, "error": "out_path escapes docs/"})
                continue
            in_path = (input_root / source_rel).resolve()
            if in_path != input_root and input_root not in in_path.parents:
                counts["error"] += 1
                errors.append({"source_path": source_rel, "error": "source_path escapes input_root"})
                continue

            # Idempotency: unchanged source → keep the existing doc, no subprocess.
            # sha256 hex is ASCII (NFC-invariant); source_rel is NFC from scout.
            if out_path.is_file():
                fm = read_frontmatter(out_path)
                existing = str(fm.get("source_sha256") or "").strip().lower()
                scout_sha = str(f.get("sha256") or "").strip().lower()
                if existing and scout_sha and existing == scout_sha:
                    counts["unchanged"] += 1
                    if not args.quiet:
                        log(f"unchanged {source_rel}")
                    continue

            # Per-file Popo routing decision (mineru only — Popo consumes the
            # mineru raw cache) and the safe per-file mineru CLI flags.
            popo_pref = f.get("popo")
            popo_in_effect = (
                (popo_pref if isinstance(popo_pref, bool) else popo_default)
                and strategy == "mineru"
            )
            if isinstance(popo_pref, bool) and popo_pref and strategy != "mineru":
                log(f"note: {source_rel} has popo=true but strategy={strategy} — "
                    "Popo only applies to mineru output; ignoring")
            extra_flags: list[str] = []
            if strategy == "mineru":
                extra_flags, fwarns = _mineru_flags(f, force_keep_raw=popo_in_effect)
                for w in fwarns:
                    log(f"{source_rel}: {w}")

            rc, parsed, stderr_tail = _run_extractor(
                strategy, in_path, out_path, doc_id, source_rel, args.timeout,
                extra_flags=extra_flags)

            if rc == "timeout":
                counts["error"] += 1
                errors.append({"source_path": source_rel, "error": stderr_tail})
                if not args.quiet:
                    log(f"error {source_rel}: {stderr_tail}")
                continue

            if rc == 2:
                # Child exit 2 == "needs install" — NEVER the driver's own exit
                # 2, and never an error/corrupt classification.
                install_hint = (parsed or {}).get("reason") or stderr_tail or "needs install (converter/CLI absent)"
                needs_attention.append({
                    "id": doc_id, "source_path": source_rel, "reason": "needs_install",
                    "doc": None, "extracted_but_flagged": False, "install_hint": install_hint,
                })
                counts["needs_attention"] += 1
                if not args.quiet:
                    log(f"needs_install {source_rel}")
                continue

            ok = rc == 0 and isinstance(parsed, dict) and parsed.get("ok") is True
            if not ok:
                reason = parsed.get("reason") if isinstance(parsed, dict) else None
                if not reason:
                    reason = (f"exit {rc}: {stderr_tail[:200]}" if stderr_tail
                              else f"exit {rc}: unparseable extractor output")
                counts["error"] += 1
                errors.append({"source_path": source_rel, "error": reason})
                if not args.quiet:
                    log(f"error {source_rel}: {reason}")
                continue

            # --- ok:true ---
            warns = [w for w in (parsed.get("warnings") or []) if isinstance(w, str)]
            mangled = [w for w in warns if classify_pdf_warning(w) == "visual_transcription"]
            dropped = [w for w in warns if classify_pdf_warning(w) == "dropped_pictures_residual"]
            unknown = [w for w in warns if classify_pdf_warning(w) is None]
            rel_doc = str(out_path.relative_to(kb_root))

            if unknown:
                unclassified.append({"id": doc_id, "source_path": source_rel, "warnings": unknown})

            if mangled:
                # mangled supersedes dropped: manual transcription covers both.
                needs_attention.append({
                    "id": doc_id, "source_path": source_rel, "reason": "visual_transcription",
                    "doc": rel_doc, "extracted_but_flagged": True,
                    "warning": "; ".join(mangled + dropped),
                })
                extracted_but_flagged += 1
            elif dropped:
                pages = residual_picture_pages(read_body(out_path))
                needs_attention.append({
                    "id": doc_id, "source_path": source_rel, "reason": "dropped_pictures_residual",
                    "doc": rel_doc, "extracted_but_flagged": True,
                    "pages": pages, "warning": "; ".join(dropped),
                })
                extracted_but_flagged += 1

            # Count extracted last — after classification — so an unexpected
            # throw above routes to the error bucket without double-counting.
            counts["extracted"] += 1
            if popo_in_effect:
                popo_targets.append(doc_id)
            if args.normalize:
                _normalize(out_path, args.timeout)
            if not args.quiet:
                flag = " [needs_attention]" if (mangled or dropped) else ""
                log(f"extracted {source_rel}{flag}")
        except Exception as e:  # noqa: BLE001 — one malformed entry must never abort the batch
            counts["error"] += 1
            errors.append({"source_path": label, "error": f"dispatch crashed: {type(e).__name__}: {e}"})
            if not args.quiet:
                log(f"error {label}: dispatch crashed: {e}")
            continue

    # Env-gated stage 2: route every mineru-extracted doc through Popo. Runs
    # only when DOC2KB_ALWAYS_POPO (or a per-file popo=true) put docs in the
    # queue — the default path leaves popo_targets empty and runs nothing.
    popo: list[dict] | None = None
    if popo_targets:
        popo = _run_popo(kb_root, popo_targets, needs_attention, args.quiet)

    # Write errors.json — overwrite even when empty so build_manifest never
    # reads a stale error list from a previous run.
    errors_path = logs_dir / "errors.json"
    errors_path.write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")

    bucket_sum = sum(counts.values())
    total = len(files)
    if bucket_sum != total:
        log(f"WARNING: bucket accounting {bucket_sum} != total files {total} "
            "(this is a driver bug — please report)")

    summary = {
        "ok": counts["error"] == 0,
        "kb_root": str(kb_root),
        "scout": str(scout_path),
        "counts": counts,
        "extracted_but_flagged": extracted_but_flagged,
        "needs_attention": needs_attention,
        "unclassified_warnings": unclassified,
        "popo": popo,
        "errors_log": str(errors_path) if errors else None,
        "elapsed_seconds": round(time.time() - t0, 2),
        "driver": driver,
    }
    _emit(summary)
    return 3 if counts["error"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
