#!/usr/bin/env python3
"""
extract_doc.py — Phase 4 extractor for legacy binary Word (.doc) files.

CLI:
    extract_doc.py <input_doc> <output_md>
                   [--doc-id doc-NNN]
                   [--source-rel relative/path/in/corpus.doc]
                   [--assets-dir <abs_path>]
                   [--assets-rel <rel_prefix_from_md>]
                   [--no-extract-images]
                   [--force-mammoth]
                   [--converter auto|soffice|textutil|antiword]

Effect:
    Writes <output_md> with YAML frontmatter + Markdown body. Stdout receives
    a single JSON line summarizing the result.

Why a converter cascade:
    The legacy `.doc` format is an OLE2/CFB binary blob — there is no
    lightweight pure-Python reader (python-docx only handles the OOXML
    `.docx`). We therefore shell out to whatever document converter the host
    provides, in descending fidelity order:

      1. `soffice` / `libreoffice --headless --convert-to docx`  → reuse the
         full `extract_docx` pipeline (tables, images, OOXML math, tracked
         changes). Cross-platform, highest fidelity.
      2. `textutil -convert docx` (macOS native)                → same docx
         pipeline. No LibreOffice install needed on a Mac.
      3. `antiword`                                             → plain text
         only (loses images/tables). Classic lightweight Linux fallback.

    None of these are pip-installable, so — like the opt-in mineru CLI — when
    no converter is found the script exits 2 with an install hint. The parent
    loop must treat exit 2 as "needs a converter installed", NOT as a corrupt
    file, and log it to `_logs/errors.json` rather than silently skipping.

The first two routes convert `.doc → .docx` into a tempdir and then call
`extract_docx.extract()`, so every DOCX feature (image extraction to
`<kb_dir>/assets/`, pandoc-for-math routing, `--force-mammoth`) applies
unchanged. The `extraction_method` frontmatter records the full chain, e.g.
`libreoffice+mammoth+markdownify@<ver>` or `textutil+pandoc@<ver>`.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import extract_docx  # sibling script, importable via the venv .pth

from _common import (  # noqa: E402
    add_assets_args,
    clean_whitespace,
    count_tokens,
    emit_failure,
    emit_success,
    resolve_assets_dir,
    sanitize_heading,
    sha256_of,
    today_iso,
    tool_version_string,
    validate_source_rel,
    write_md,
)


MIN_BODY_CHARS = 50
# soffice/textutil cold-start + convert on a typical .doc is a few seconds;
# 180 s is generous and still catches a wedged headless LibreOffice.
CONVERT_TIMEOUT_SEC = 180

INSTALL_HINT = (
    "no .doc converter found on PATH. Install one of: LibreOffice "
    "(`brew install --cask libreoffice` / `apt install libreoffice-writer`, "
    "provides `soffice`), or `antiword` (`apt install antiword`). macOS also "
    "ships `textutil`. Then re-run."
)


def _which_soffice() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def resolve_converter(choice: str) -> tuple[str, str] | None:
    """Return (kind, binary) for the chosen converter, or None if unavailable.

    kind ∈ {"soffice", "textutil", "antiword"}. `auto` tries them in
    descending fidelity order."""
    if choice in ("auto", "soffice"):
        soffice = _which_soffice()
        if soffice:
            return "soffice", soffice
        if choice == "soffice":
            return None
    if choice in ("auto", "textutil"):
        textutil = shutil.which("textutil")
        if textutil:
            return "textutil", textutil
        if choice == "textutil":
            return None
    if choice in ("auto", "antiword"):
        antiword = shutil.which("antiword")
        if antiword:
            return "antiword", antiword
        if choice == "antiword":
            return None
    return None


def _convert_to_docx_soffice(binary: str, input_doc: Path, out_dir: Path) -> Path:
    """LibreOffice headless .doc → .docx. Uses a throwaway UserInstallation
    profile so the convert never blocks on / collides with a running LO GUI
    instance (the classic headless-convert silent-failure)."""
    profile = out_dir / "lo-profile"
    cmd = [
        binary, "--headless", "--norestore",
        f"-env:UserInstallation=file://{profile}",
        "--convert-to", "docx", "--outdir", str(out_dir),
        str(input_doc),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, timeout=CONVERT_TIMEOUT_SEC, check=False,
    )
    produced = sorted(out_dir.glob("*.docx"))
    if not produced:
        stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
        stdout = (proc.stdout or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(
            f"libreoffice produced no .docx (exit {proc.returncode}): "
            f"{(stderr or stdout)[:200]}"
        )
    return produced[0]


def _convert_to_docx_textutil(binary: str, input_doc: Path, out_dir: Path) -> Path:
    """macOS `textutil` .doc → .docx."""
    out = out_dir / "converted.docx"
    cmd = [binary, "-convert", "docx", "-output", str(out), str(input_doc)]
    proc = subprocess.run(
        cmd, capture_output=True, timeout=CONVERT_TIMEOUT_SEC, check=False,
    )
    if not out.is_file():
        stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(
            f"textutil produced no .docx (exit {proc.returncode}): {stderr[:200]}"
        )
    return out


def _extract_via_antiword(binary: str, input_doc: Path) -> tuple[str, list[str]]:
    """Plain-text fallback. antiword writes the document text to stdout;
    no images or table structure survive. Returns (body, warnings)."""
    warnings: list[str] = []
    proc = subprocess.run(
        [binary, str(input_doc)],
        capture_output=True, timeout=CONVERT_TIMEOUT_SEC, check=False,
    )
    if proc.returncode != 0 and not proc.stdout:
        stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(
            f"antiword exited {proc.returncode}: {stderr[:200]}"
        )
    text = proc.stdout.decode("utf-8", "replace")
    warnings.append(
        "extracted via antiword (plain text) — tables and images are lost. "
        "Install LibreOffice (`soffice`) for full-fidelity .doc extraction"
    )
    return clean_whitespace(text), warnings


def extract(input_doc: Path,
            *,
            converter: str = "auto",
            doc_id: str = "doc-000",
            assets_dir: Path | None = None,
            assets_rel_prefix: str = "../assets",
            extract_images: bool = True,
            force_mammoth: bool = False) -> tuple[str, dict, list[str], str]:
    """Returns (markdown_body, frontmatter_extras, warnings, extraction_method).

    Routes through a converter cascade — see the module docstring. Raises
    RuntimeError("no converter") when none is available so main() can exit 2
    with the install hint."""
    resolved = resolve_converter(converter)
    if resolved is None:
        raise RuntimeError("no converter")
    kind, binary = resolved
    warnings: list[str] = []

    if kind in ("soffice", "textutil"):
        with tempfile.TemporaryDirectory(prefix="doc2kb-doc-") as td:
            out_dir = Path(td)
            if kind == "soffice":
                converted = _convert_to_docx_soffice(binary, input_doc, out_dir)
                conv_label = "libreoffice"
            else:
                converted = _convert_to_docx_textutil(binary, input_doc, out_dir)
                conv_label = "textutil"
            # Reuse the full DOCX pipeline on the converted file.
            body, extras, w, inner = extract_docx.extract(
                converted,
                doc_id=doc_id,
                assets_dir=assets_dir,
                assets_rel_prefix=assets_rel_prefix,
                extract_images=extract_images,
                force_mammoth=force_mammoth,
            )
            warnings.extend(w)
            method = f"{conv_label}+{inner}"
            return body, extras, warnings, method

    # antiword — plain text only.
    body, w = _extract_via_antiword(binary, input_doc)
    warnings.extend(w)
    if len(body.strip()) < MIN_BODY_CHARS:
        warnings.append(f"extracted body is unusually short ({len(body)} chars)")
    headings = []
    for line in body.split("\n"):
        if line.startswith("# ") and len(line) < 200:
            sanitized = sanitize_heading(line[2:])
            if sanitized:
                headings.append(sanitized)
            if len(headings) >= 10:
                break
    extras = {
        "paragraphs": 0,
        "inline_images": 0,
        "has_tables": False,
        "has_equations": False,
        "has_tracked_changes": False,
        "headings": headings,
    }
    return body, extras, warnings, "antiword"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extract legacy .doc → Markdown via a converter cascade "
                    "(soffice/textutil → DOCX pipeline, or antiword → text)."
    )
    ap.add_argument("input_doc")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    add_assets_args(ap)
    ap.add_argument("--force-mammoth", action="store_true",
                    help="Pass through to the DOCX pipeline: bypass the "
                         "pandoc-for-math route even when math is present.")
    ap.add_argument("--converter", choices=["auto", "soffice", "textutil", "antiword"],
                    default="auto",
                    help="Force a specific converter (default: auto cascade).")
    args = ap.parse_args()

    in_path = Path(args.input_doc).expanduser().resolve()
    out_path = Path(args.output_md).expanduser().resolve()
    source_rel = args.source_rel or in_path.name
    try:
        source_rel = validate_source_rel(source_rel)
    except ValueError as e:
        emit_failure(f"invalid --source-rel: {e}")
        return 1

    if not in_path.is_file():
        emit_failure(f"input not found: {in_path}")
        return 1

    assets_dir: Path | None = resolve_assets_dir(args, out_path)

    try:
        body, extras, warnings, method = extract(
            in_path,
            converter=args.converter,
            doc_id=args.doc_id,
            assets_dir=assets_dir,
            assets_rel_prefix=args.assets_rel,
            extract_images=not args.no_extract_images,
            force_mammoth=args.force_mammoth,
        )
    except RuntimeError as e:
        if str(e) == "no converter":
            # Exit 2, mirroring extract_pdf_mineru.py's "needs install"
            # contract — the parent loop must not treat this as corrupt.
            emit_failure(INSTALL_HINT, extra={"input": str(in_path), "needs_install": True})
            return 2
        emit_failure(f"extraction failed: {e}", extra={"input": str(in_path)})
        return 1
    except Exception as e:
        emit_failure(f"extraction failed: {e}", extra={"input": str(in_path)})
        return 1

    fm = {
        "id": args.doc_id,
        "source": source_rel,
        "source_type": "doc",
        "source_sha256": sha256_of(in_path),
        "extraction_method": f"{method}@{tool_version_string()}",
        "extraction_date": today_iso(),
        "paragraphs": extras.get("paragraphs", 0),
        "inline_images": extras.get("inline_images", 0),
        "has_tables": extras.get("has_tables", False),
        "has_equations": extras.get("has_equations", False),
        "has_tracked_changes": extras.get("has_tracked_changes", False),
        "headings": extras.get("headings", []),
        "tokens_estimated": count_tokens(body),
        "warnings": warnings,
    }
    if extras.get("assets"):
        fm["assets"] = extras["assets"]
    write_md(out_path, fm, body)
    emit_success(out_path, body, warnings, extra={
        "inline_images": extras.get("inline_images", 0),
        "has_tables": extras.get("has_tables", False),
        "extractor": method,
        "assets_extracted": len(extras.get("assets", [])),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
