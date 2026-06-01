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

## Preferred: the batch dispatcher (`extract_corpus.py`)

Do NOT hand-loop the per-file recipes below in normal operation. Run the
Phase-4 dispatcher once — it reads `_scout.json`, applies the strategy→script
map for you, writes every `docs/<id>-<slug>.md` + `_logs/errors.json`, and
returns one JSON summary:

```bash
python3 <skill_dir>/scripts/ensure_env.py extract_corpus.py <kb_dir> \
    [--timeout 600] [--normalize] [--quiet]
```

Summary shape (last stdout line):

```json
{
  "ok": true,
  "counts": {"extracted": N, "unchanged": N, "skipped_by_decision": N,
             "error": N, "needs_attention": N},
  "extracted_but_flagged": N,
  "needs_attention": [ /* see format-spec.md */ ],
  "unclassified_warnings": [ {"id","source_path","warnings":[...]} ],
  "errors_log": "<kb>/_logs/errors.json" | null
}
```

- Idempotent: a file whose produced doc already has a matching
  `source_sha256` is counted `unchanged` and not re-extracted.
- Refuses to start (exit 2) if any `_scout.files[]` entry still has a non-null
  `action_required` — resolve Phase 3 first (set the final `extraction_strategy`
  AND null `action_required`).
- Exit 0 = all terminal (needs_attention is not failure); 2 = refused; 3 = had errors.
- After it returns, act on `needs_attention[]` (`needs_install` → install the
  converter/CLI and re-run; `visual_transcription` / `dropped_pictures_residual`
  → mineru page-patch or Read-tool transcription), then run `build_manifest.py`.

The per-file recipes below are what the dispatcher invokes internally, and are
the right tool for **ad-hoc** single-file work (mineru page-patch, manual
re-extraction). Don't use them to replace the dispatcher for a whole corpus.

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
| `doc`               | `extract_doc.py`                | `<input> <output> --doc-id <id> --source-rel <rel>` (legacy binary `.doc`; converter cascade `soffice`/`libreoffice` → `.docx` → DOCX pipeline, macOS `textutil` same path, else `antiword` → text. Force one with `--converter soffice\|textutil\|antiword`. **Exits 2 with an install hint when no converter is on PATH** — treat as "needs install", not corrupt, same as the mineru CLI) |
| `rtf`               | `extract_rtf.py`                | `<input> <output> --doc-id <id> --source-rel <rel>` (pandoc when on PATH — preserves tables/images/structure; else pure-Python `striprtf` → plain text. Force the fallback with `--force-striprtf`) |
| `python-pptx`       | `extract_pptx.py`               | `<input> <output> --doc-id <id> --source-rel <rel>` |
| `passthrough-md`    | `extract_md_txt.py --mode md`   | `<input> <output> --doc-id <id> --source-rel <rel> --mode md` |
| `passthrough-txt`   | `extract_md_txt.py --mode txt`  | `<input> <output> --doc-id <id> --source-rel <rel> --mode txt` |
| `trafilatura`       | `extract_html.py`               | `<input> <output> --doc-id <id> --source-rel <rel>` |
| `ipynb`             | `extract_ipynb.py`              | `<input> <output> --doc-id <id> --source-rel <rel>` |
| `skip`              | (none — file is skipped) | — |
| `needs_password`    | (Phase 3 user decision; if password given, re-classify and use `pymupdf4llm`) | — |
| `needs_ocr_or_vlm`  | (Phase 3 user decision; pick `vlm_mlx` to route through `mineru` strategy — requires opt-in tier; default behaviour is `skip`) | — |
| `not_in_mvp`        | (XLSX/EPUB/ODT/image — Phase 3 user decision; skip in MVP) | — |

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

## Page-targeted patching with mineru

When a `pymupdf4llm` extraction emits a `dropped_pictures` /
`mangled_visual_layout` warning that names a handful of specific pages
(usually formulas, schemas, or text-as-vector diagrams the text-layer
extractor could not recover), use `extract_pdf_mineru.py` in page-patch
mode instead of re-extracting the whole PDF. mineru's VLM run on a
50-page book is hours; running it on the 8 broken pages takes a
minute.

```bash
python3 "$SKILL/scripts/ensure_env.py" extract_pdf_mineru.py \
    "$INPUT" /unused/output/path.md \
    --doc-id "$DOCID" --source-rel "$SREL" \
    --pages "2,18-19,35,221,243-244,588" \
    --patch-into "$KB/docs/$DOCID-<slug>.md" \
    --lang cyrillic
```

What the script does:

1. Slices the original PDF down to just the requested pages via pymupdf
   in a tempdir — mineru sees a tiny subset PDF and processes only that.
2. Remaps every `[page N]` anchor and every asset filename it emits
   from mineru's subset indices back to the original PDF page numbers.
   Asset names get an extra `-mineru-` infix
   (`<doc_id>-page<orig:03d>-mineru-imgN.<ext>`) so they never collide
   with pymupdf4llm's existing `<doc_id>-page<NN>-imgN` files.
3. Splices the new page sections into the existing target md, replacing
   only the `[page N]` blocks listed in `--pages`. Everything else
   (preamble, other pages, frontmatter primary fields) is untouched.
4. Updates the target's frontmatter with `mineru_patched_pages: [...]`
   (sorted union across runs) and `extraction_method_supplementary:
   mineru-<backend>@<version>`. Appends a single-line splice-summary
   warning, accumulates the mineru-side warnings under
   `mineru_patch[…]:` prefixes.
5. Safety: refuses to splice if the target's `source_sha256` ≠ the
   input PDF's sha256 (use `--force-patch` to override, only for known
   re-exports). Refuses if `--patch-into` is set but `--pages` is not.

Without `--patch-into`, the same `--pages` invocation writes a
standalone patches md (still with original page numbers, no subset
indices anywhere) so you can review before splicing.

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
