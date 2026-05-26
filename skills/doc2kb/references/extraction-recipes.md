# doc2kb — Extraction Recipes

Lookup table from `extraction_strategy` (as emitted by `scout_corpus.py`) to
the script and arguments you should invoke.

All commands assume:
- `SKILL=/path/to/skills/doc2kb` (the skill folder containing `SKILL.md`)
- `KB=/path/to/output/kb` (the kb_dir argument from scout)
- `INPUT=/abs/path/to/source/file.ext` (the absolute source path)
- `DOCID=doc-NNN` (from `_scout.files[].id`)
- `SREL=rel/source/path.ext` (from `_scout.files[].source_path`)
- The slug is derived from the source filename — `_common.py` exposes
  `kb_doc_filename(doc_id, source_path)` for convenience, or just construct
  as `<DOCID>-<slugify(stem)>.md` (slugify: NFKD-strip → drop non-ASCII →
  collapse non-alnum to `-` → lowercase, ≤48 chars).

The canonical invocation uses `ensure_env.py` as a wrapper — it bootstraps
the venv on first call and execs the target script through `.venv/bin/python`:

```python
import json, subprocess
out_name = f"{DOCID}-{slugify(Path(SREL).stem)}.md"
out_path = f"{KB}/docs/{out_name}"
result = subprocess.run([
    "python3",
    f"{SKILL}/scripts/ensure_env.py",
    SCRIPT,                     # e.g. "extract_pdf_pymupdf4llm.py"
    INPUT, out_path,
    "--doc-id", DOCID,
    "--source-rel", SREL,
], capture_output=True, text=True, check=False)
if result.returncode != 0:
    # log to _logs/errors.json — never crash the loop
    ...
payload = json.loads(result.stdout.strip().splitlines()[-1])
assert payload["ok"]
```

## Strategy → script map

| extraction_strategy | script                          | invocation |
|---------------------|---------------------------------|------------|
| `pymupdf4llm`       | `extract_pdf_pymupdf4llm.py`    | `<input> <output> --doc-id <id> --source-rel <rel>` (auto image extraction to `<kb_dir>/assets/`; override with `--assets-dir`/`--assets-rel`, disable with `--no-extract-images`) |
| `mineru`            | `extract_pdf_mineru.py`         | **Opt-in tier** — requires `ensure_env.py --tier mineru` once. `<input> <output> --doc-id <id> --source-rel <rel>` (default backend `auto`, language `cyrillic`; pass `--backend pipeline` for CPU-only, `--lang en` for English-only; `--keep-raw` preserves MinerU output under `<kb_dir>/_mineru/<doc_id>/` for follow-up `postprocess_popo.py`). Exits 2 with install hint when mineru CLI is missing — parent loop should mark the file as needing install rather than skipping silently. |
| `mammoth`           | `extract_docx.py`               | `<input> <output> --doc-id <id> --source-rel <rel>` (auto-routes to `pandoc` when `has_equations: true` and pandoc is on PATH; force mammoth with `--force-mammoth`) |
| `python-pptx`       | `extract_pptx.py`               | `<input> <output> --doc-id <id> --source-rel <rel>` |
| `passthrough-md`    | `extract_md_txt.py --mode md`   | `<input> <output> --doc-id <id> --source-rel <rel> --mode md` |
| `passthrough-txt`   | `extract_md_txt.py --mode txt`  | `<input> <output> --doc-id <id> --source-rel <rel> --mode txt` |
| `trafilatura`       | `extract_html.py`               | `<input> <output> --doc-id <id> --source-rel <rel>` |
| `ipynb`             | `extract_ipynb.py`              | `<input> <output> --doc-id <id> --source-rel <rel>` |
| `skip`              | (none — file is skipped) | — |
| `needs_password`    | (Phase 3 user decision; if password given, re-classify and use `pymupdf4llm`) | — |
| `needs_ocr_or_vlm`  | (Phase 3 user decision; pick `vlm_mlx` to route through `mineru` strategy — requires opt-in tier; default behaviour is `skip`) | — |
| `not_in_mvp`        | (XLSX/EPUB/RTF/ODT/image — Phase 3 user decision; skip in MVP) | — |

## Opt-in MinerU tier

The `mineru` strategy is opt-in by design: heavy ML deps and a ~3 GB model
download never enter the lightweight default tier. To enable:

```bash
# One-time, installs mineru[all] + MLX wheels on Apple Silicon
python3 <skill_dir>/scripts/ensure_env.py --tier mineru

# Auto-route image_only PDFs through mineru when scanning a corpus
python3 <skill_dir>/scripts/ensure_env.py scout_corpus.py \
    <input_dir> <kb_dir> --enable-mineru
```

Without `--enable-mineru` scout behaves exactly as before — `image_only`
PDFs surface as the `scanned_pdf` user-decision group and default to
`skip`. The flag is also recorded in `_scout.flags.enable_mineru` so
follow-up tools see the choice.

## Stage 2 (optional): MinerU-Popo post-processing

If long-document hierarchy / cross-page table merging still looks wrong
after MinerU, the opt-in `postprocess_popo.py` script runs the upstream
MinerU-Popo pipeline over the cached `<kb_dir>/_mineru/<doc_id>/`
artefacts and writes a document tree as a sidecar JSON next to each kb
doc. See `postprocess_popo.py --help` and the SKILL.md "Optional stage 2"
section for the exact setup steps (the Popo conda env and HF model
download are handled by the user, not by doc2kb).

## Post-extraction normalization

For every successfully extracted `.md`, optionally invoke `normalize_md.py
--write`:

```bash
python3 "$SKILL/scripts/ensure_env.py" normalize_md.py "$KB/docs/$OUT_NAME" --write
```

This is **safe**: idempotent, never summarizes, only removes recurring
headers/footers and matches known boilerplate regexes. Returns a JSON
report with `chars_saved` and `removed_counts`.

## Error handling

Each extract script returns JSON to stdout. Always parse it:

```python
payload = json.loads(result.stdout)
if not payload["ok"]:
    # Log to _logs/errors.json — do not crash the pipeline.
    errors.append({"source_path": SREL, "error": payload["reason"]})
    continue
warnings = payload.get("warnings", [])
if warnings:
    # Don't fail — just surface to the user when summarizing the corpus.
    pass
```

When all files are processed, write `<kb_dir>/_logs/errors.json` with the
collected errors. `build_manifest.py` will pick it up and surface it in
`manifest.json.errors[]`.

## Re-extraction

`source_sha256` in each `.md` frontmatter is the cache key. If you re-run
on the same source file, compare its sha256 against the existing doc; if
unchanged, you can skip re-extraction. (build_manifest does NOT enforce
this — the loop logic lives in agent code.)
