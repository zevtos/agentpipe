# Rubric: 02_table_heavy_pdf

## Pass criteria (all must hold)
- [ ] Process exits 0
- [ ] `ok == true` in the emitted JSON
- [ ] `extractor == "pymupdf4llm+docling"`
- [ ] `docling_pages` is non-empty (length >= 2 — surveys have multiple table pages)
- [ ] `docling_pages` length is `<= 30` (DOCLING_MAX_PAGES_PER_DOC cap respected)
- [ ] Every page index in `docling_pages` corresponds to a page that contained `> 8` pipes in the pymupdf4llm-only baseline (verify by re-running with `DOCLING_DISABLE=1` and counting pipes per page in the baseline output)
- [ ] `docling_skipped == false`
- [ ] `warnings` does NOT contain `docling_unavailable` or `docling_models_download_failed`
- [ ] The stitched `text` contains at least one well-formed markdown table on at least one of the `docling_pages` page indices (verify by manual inspection — see grep snippet below)
- [ ] Markdown table rows on docling-re-parsed pages have **consistent cell counts** (the number of `|` separators per row inside one table block is uniform — a hallmark of correct table structure)
- [ ] `text` length is within ±50% of the pymupdf4llm-only baseline (tables may legitimately expand or contract under re-parse, but a 10× change indicates corruption)
- [ ] No page index in `docling_pages` appears twice (no duplicate stitching)
- [ ] Re-running with `DOCLING_DISABLE=1` produces `extractor == "pymupdf4llm"` AND markdown tables on the same pages show visibly worse structure (overflowing cells, broken alignment, fewer pipes per row)

## How to run
```
mkdir -p /tmp/ultrasearch_eval_pdfs
[ -f /tmp/ultrasearch_eval_pdfs/helm.pdf ] || \
  curl -fsSL -o /tmp/ultrasearch_eval_pdfs/helm.pdf https://arxiv.org/pdf/2211.09110.pdf

# With docling fallback
python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py parse.py \
  --pdf /tmp/ultrasearch_eval_pdfs/helm.pdf --max-pages 30 --json \
  > /tmp/parse_result_02_docling.json

# Baseline: pymupdf4llm-only
DOCLING_DISABLE=1 python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py parse.py \
  --pdf /tmp/ultrasearch_eval_pdfs/helm.pdf --max-pages 30 --json \
  > /tmp/parse_result_02_baseline.json
```

Inspect the table quality difference:
```
python3 -c "
import json
d1 = json.load(open('/tmp/parse_result_02_docling.json'))
d2 = json.load(open('/tmp/parse_result_02_baseline.json'))
print('docling extractor:', d1['extractor'], 'pages re-parsed:', len(d1.get('docling_pages', [])))
print('baseline extractor:', d2['extractor'])
print()
# Count pipes per page in baseline to identify which pages SHOULD trigger fallback
baseline_pages = d2['text'].split('\n-----\n')
table_pages = [(i, p.count('|')) for i, p in enumerate(baseline_pages) if p.count('|') > 8]
print(f'Pages with >8 pipes in baseline: {len(table_pages)}')
print(f'Pages re-parsed by docling: {d1.get(\"docling_pages\", [])}')
print()
# Spot-check a table page: print first 500 chars of both versions for visual diff
if table_pages:
    pidx = table_pages[0][0]
    print(f'=== Page {pidx} baseline (first 500 chars) ===')
    print(baseline_pages[pidx][:500])
    print()
    docling_pages = d1['text'].split('\n-----\n')
    if pidx < len(docling_pages):
        print(f'=== Page {pidx} docling (first 500 chars) ===')
        print(docling_pages[pidx][:500])
"
```

Manual inspection step — for at least 2 table pages, confirm:
- The docling version has uniform cell counts per row
- Numerical columns align (decimals at same position, headers above values)
- No cell content has bled into the wrong row/column

## Notes
This fixture requires manual visual confirmation of table quality. The
automated rubric items only verify that the heuristic fired and the
stitching didn't corrupt the document length. The human reviewer must spot-
check the tables.

Most likely failure modes:

1. **Pipe threshold off-by-one** — `> 8` vs `>= 8`. The plan specifies
   `pipes > PIPE_THRESHOLD_PER_PAGE` (strict). A table with exactly 8 pipes
   per row does NOT trigger; a row with 9 does. This is correct but worth
   confirming if pages near the threshold are not being re-parsed.
2. **Tables are caption-only** — some "tables" in PDFs are actually figures
   with captions that contain pipe characters as visual separators. The
   heuristic doesn't distinguish, and may trigger docling on figure-heavy
   pages that don't need it. Acceptable — docling on a non-table page just
   wastes a bit of compute; the output is still valid markdown.
3. **TableFormer model not bundled** — docling ships TableFormer in its
   main pip package, but some lean install paths skip it. Symptom:
   `extractor == "pymupdf4llm+docling"` but the docling pages have no
   markdown tables at all, just running text. Fix: confirm
   `pipeline_options.do_table_structure = True` is set in the
   `_docling_parse_pages` helper.
4. **Hard cap of 30 pages** — for a 150-page survey, only the first 30
   table pages get docling-re-parsed. The remaining ~120 pages stay
   pymupdf4llm. This is intentional (OOM guard, research §14 line 304), but
   if the cap is missing, docling will OOM on the full document. Verify by
   reading `_detect_problem_pages` for the `if len(out) >=
   DOCLING_MAX_PAGES_PER_DOC: break` check.
5. **`--max-pages 30` interaction with docling cap** — the user can request
   only the first 30 pages via `--max-pages`. The docling cap of 30 is an
   internal limit. The two should compose: `--max-pages 50` with 35
   table-heavy pages should re-parse exactly 30 of them, not 35 (the
   internal cap wins) and not 0 (the user's `--max-pages` doesn't reset the
   cap).
