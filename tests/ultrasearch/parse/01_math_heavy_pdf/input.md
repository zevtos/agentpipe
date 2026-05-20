# Input: math/equation-heavy PDF (docling fallback triggers)

## Target PDF

PaperQA2 paper — arXiv:2409.13740

Download via the existing skill machinery:
```
python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py fetch.py \
  --doi 10.48550/arXiv.2409.13740 --out /tmp/ultrasearch_eval_pdfs/paperqa2.pdf
```

Or directly from arXiv (no auth required, ~2 MB):
```
curl -L -o /tmp/ultrasearch_eval_pdfs/paperqa2.pdf https://arxiv.org/pdf/2409.13740.pdf
```

## Rationale

This PDF is a LaTeX-typeset arXiv preprint with multiple equation
environments — `align`, `equation`, `cases`, and inline math throughout the
methodology and evaluation sections. It is exactly the class of document
that pymupdf4llm extracts poorly: the math glyphs come through as garbled
Unicode runs (`∑`/`∫`/Greek letters mis-coded), the equation alignment is
lost, and on the worst pages the picture-placeholder + mangled-layout
heuristics fire simultaneously.

This is a perfect target for `parse.py`'s Stage 2 docling fallback (§3.4):

1. **`_split_into_pages`** must successfully split the pymupdf4llm output on
   `"\n-----\n"` page separators into per-page chunks.
2. **`_detect_problem_pages`** must identify the math-heavy pages by
   matching `MATH_MARKER_RE` (regex covers `$$`, `\begin{equation}`,
   `\begin{align}`, `\begin{matrix}`, `\begin{cases}`, `\begin{aligned}`).
   Pages with `>= 2` picture placeholders or that fail
   `_detect_mangled_layout` per-page also qualify.
3. **`page_fraction >= DOCLING_MIN_PAGE_FRACTION` (0.05)** must hold —
   PaperQA2's math sections span enough pages to clear the 5% threshold for
   a ~20-page paper. With problem pages clamped at `DOCLING_MAX_PAGES_PER_DOC
   = 30`, the cap should not fire on this document.
4. **`_docling_parse_pages`** must lazy-import `docling.document_converter`,
   run `DocumentConverter.convert(pdf_path)` once, and emit per-page
   markdown via `result.document.export_to_markdown(page_no=p+1)`.
5. **`_stitch_pages`** must replace the problem-page indices with docling's
   markdown and rejoin the document with `"\n-----\n"` separators.
6. The Stage 1 `mangled_visual_layout` and `dropped_pictures` warnings must
   be **scrubbed** from `warnings[]` after the docling fallback succeeds —
   they were placeholders for "this needs better extraction", and the
   successful re-parse satisfies that need.

The result must declare `extractor == "pymupdf4llm+docling"` and list the
re-parsed page indices in `docling_pages`. `docling_skipped` must be `false`
because docling fired successfully.

This fixture is the smoke test for the entire docling path. It catches:
docling import-time failures, MPS/CUDA accelerator crashes (research §1
line 16 notes docling auto-uses MPS, unstable on M1 8GB), per-page export
failures, page-numbering off-by-one between pymupdf4llm (0-based) and
docling (1-based), and the warning-scrub step.
