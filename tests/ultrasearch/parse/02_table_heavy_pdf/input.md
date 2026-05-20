# Input: table-heavy PDF (docling table extraction)

## Target PDF

A survey/benchmark paper with multiple markdown tables on multiple pages.
Recommended target: the OpenReview benchmark survey
`Holistic Evaluation of Language Models` (HELM), arXiv:2211.09110 — it has
extensive results tables spanning ~10 pages.

Alternative recommended targets (any of these works):
- arXiv:2211.09110 (HELM survey) — heaviest table coverage
- arXiv:2304.13712 (Survey of LLMs) — many comparison tables
- arXiv:2306.13549 (Survey of multi-modal LLMs) — table-rich

Download (HELM is large, ~150 pages; restrict to first 30 for speed):
```
mkdir -p /tmp/ultrasearch_eval_pdfs
curl -fsSL -o /tmp/ultrasearch_eval_pdfs/helm.pdf https://arxiv.org/pdf/2211.09110.pdf
```

If `parse.py` supports `--max-pages 30`, use it to bound the run. If not,
prefer the smaller `arXiv:2306.13549` paper (~50 pages) which still hits
the table heuristic on enough pages.

## Rationale

This fixture exercises the **pipe-count branch** of `_detect_problem_pages`:

```python
if pipes > PIPE_THRESHOLD_PER_PAGE  # 8
   or has_math
   or mangled is not None
   or n_pictures >= 2:
    out.append(i)
```

`PIPE_THRESHOLD_PER_PAGE = 8` from research §1 line 19 (Stage 2 plan
§3.4.1). pymupdf4llm emits markdown tables with `|` separators between
cells; a wide results table with 10 columns × N rows produces 10 `|` per
row, easily clearing the 8-pipe threshold on multiple pages.

Why docling matters for tables specifically: pymupdf4llm's table detection
is text-position-based, which means it produces tables that are
**structurally correct in markdown** but often have:
- Cell content overflowing into the next cell when the column is narrow
- Header row merged with the first data row
- Multi-line cells (e.g. captions inside cells) broken into adjacent rows
- Numerical alignment lost (decimals shifted)

docling, by contrast, uses its TableFormer transformer model
(`PdfPipelineOptions(do_table_structure=True)`) which understands table
**structure** — including cell spans, header recognition, and proper
multi-line cell handling. The Stage 2 fallback is supposed to re-parse
table-heavy pages with this model so the resulting markdown tables are
genuinely usable downstream (e.g. by `index.py` chunking, by `synthesize.py`
LLM prompts).

This fixture catches:

1. **Table heuristic doesn't fire** — if `_detect_problem_pages` counts
   pipes incorrectly (e.g. uses `>` instead of `>=`, off-by-one, or counts
   only the first occurrence), table pages slip through and no docling
   re-parse happens. Symptom: `extractor == "pymupdf4llm"` on a paper with
   obvious tables.
2. **docling re-parse silently fails on tables** — docling's
   `do_table_structure=True` option requires the TableFormer model
   (downloaded with the main docling bundle). If it's not loaded,
   tables get emitted as a single text blob instead of markdown. Symptom:
   `extractor == "pymupdf4llm+docling"` but the table pages in `text`
   contain runs of cell content with no `|` separators.
3. **Stitching corrupts table boundaries** — if `_stitch_pages` joins on the
   wrong delimiter (e.g. single `\n` instead of `\n-----\n`), the boundary
   between docling's table and the next page's pymupdf4llm content breaks
   markdown table parsing downstream.
4. **OOM hard cap fires when it shouldn't** — `DOCLING_MAX_PAGES_PER_DOC =
   30`. If a 150-page survey has 50 table-heavy pages, only the first 30
   should be docling'd and pages 31+ should retain pymupdf4llm content.
   Verify by checking `docling_pages` length <= 30.

Unlike scenario 01, this fixture requires manual inspection of the markdown
output to confirm table quality — there is no automatic way to measure
"are these tables better than baseline?" without a reference parse. The
rubric includes a comparison step where the pymupdf4llm-only baseline (via
`DOCLING_DISABLE=1`) is diffed against the docling-augmented output, and
the human reviewer eyeballs a handful of tables in both versions.
