# PDF parsing — troubleshooting (ultrasearch Stage 1)

## Default: `pymupdf4llm`

Stage 1 uses **only** `pymupdf4llm` (see `scripts/parse.py`). Stage 2 will add a docling fallback for math/table-heavy pages, gated by a per-page heuristic.

### License — AGPL v3

`pymupdf4llm` transitively pins `pymupdf`, both released by Artifex under **GNU AGPL v3** (same as MuPDF). This is fine for an open-source skill — agentpipe is MIT-licensed but a single AGPL dependency does **not** taint MIT code; it does mean:

- **Reuse of `pymupdf4llm` in a closed-source product requires a commercial license from Artifex.**
- The ultrasearch skill itself is fine to share, redistribute, embed in research workflows.
- See PyMuPDF Discussion #971 for the canonical Artifex statement on this.

If a future user needs a non-AGPL parser, Stage 3 `--parser docling` is the supported alternative (Apache-2 from IBM via LF AI & Data).

## Apple Silicon (MPS) device detection

The embedding step in `index.py` will use MPS automatically on M-series Macs:

```python
import torch
torch.backends.mps.is_available()   # → True on M1/M2/M3/M4
```

If MPS is unavailable (older macOS, Intel Mac, or a corrupt PyTorch build), the embedder falls back to CPU at ~10× slowdown. Check with:

```bash
~/.claude/skills/ultrasearch/.venv/bin/python -c \
  "import torch; print('mps:', torch.backends.mps.is_available(), 'cuda:', torch.cuda.is_available())"
```

Expected on a healthy M-series setup: `mps: True cuda: False`.

## Common failure modes

### `pdf_empty` / `pdf_not_found` / `not_a_pdf`

`parse.py` validates the file before invoking pymupdf4llm:

- `pdf_not_found` — caller passed a wrong path. Check `data/cache/pdfs/`.
- `pdf_empty` — 0-byte file. Likely a botched download; clear the cache (`rm` the file) and re-run.
- `not_a_pdf` — first 4 bytes are not `%PDF`. Usually an HTML interstitial (CAPTCHA, publisher login wall) that slipped past `fetch.py`'s magic-byte check; the magic-byte check is layered for defense in depth.

### `empty_or_image_only_pdf`

Text body is < 100 chars after extraction. The PDF is either:

1. **A scanned image** — no text layer. Stage 1 does **not** OCR; the orchestrator falls back to indexing the abstract only if `c.abstract` is set. The paper is still upserted into `papers` with `last_indexed_at` so re-runs can be skipped.
2. **Encrypted with text-extraction disabled** — same outcome as scanned.

If you want OCR for scanned PDFs, use `doc2kb` (which has a marker/docling tier) instead.

### `mangled_visual_layout`

Symptom: a markdown table whose cells contain runs of `single-char + <br> + single-char + <br>…`. This is `pymupdf4llm` collapsing positioned math (subscripts, fraction bars, primes) into an orphan-fragment table.

Stage 1 emits a warning but keeps the (broken) text. Stage 2 will re-parse the affected pages with docling, which handles positioned math correctly. Workaround: skip the offending paper or read it via Claude's `Read` tool, which renders the PDF visually.

### `dropped_pictures:N`

Symptom: `pymupdf4llm` emitted `==> picture [WxH] intentionally omitted <==` placeholders for math-heavy pages. The body text references "equation (3)" but the equation itself is gone.

Stage 1 strips the placeholders and surfaces a warning if the per-page density crosses 2.0 or absolute count crosses 5. Stage 2 docling fallback handles this — for now, the paper is indexed but its math content is missing.

### `pdf_encrypted`

`pymupdf4llm` raises an "encrypted" exception. Stage 1 does **not** handle passwords. The paper is **not** indexed; orchestrator marks it as `ok: False, reason: pdf_encrypted`.

### `pymupdf4llm not importable`

Bootstrap failure. Run:

```bash
python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py
```

If it still fails, delete `~/.claude/skills/ultrasearch/.venv/` and run the bootstrap again.

## Per-page heuristic (Stage 2 preview)

Stage 2 will compute, per page:

- `pipe_count = body.count('|')` — table density
- `math_marker = re.search(r'\\$\\$|\\\\begin\\{(equation|align|matrix)\\}', body)`

If either crosses the threshold, that page is re-parsed by docling and stitched into the pymupdf4llm output. Expected speedup vs docling-only: ~5× (research §1 line 19).

## Stage-specific behavior

| Stage | Default extractor | Fallback |
|---|---|---|
| Stage 1 | pymupdf4llm only | none (warnings only) |
| Stage 2 | pymupdf4llm + per-page docling for tables/math | none |
| Stage 3 | runtime model selection by query language | + `--marker` opt-in |
