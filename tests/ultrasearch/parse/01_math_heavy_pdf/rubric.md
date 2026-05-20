# Rubric: 01_math_heavy_pdf

## Pass criteria (all must hold)
- [ ] Process exits 0
- [ ] `ok == true` in the emitted JSON
- [ ] `extractor == "pymupdf4llm+docling"` (NOT just `"pymupdf4llm"`)
- [ ] `docling_pages` is a non-empty list of 0-based page indices (length >= 1)
- [ ] Every entry in `docling_pages` is an integer `>= 0` and `< pages`
- [ ] `docling_skipped == false`
- [ ] `warnings` does NOT contain any entry starting with `mangled_visual_layout` (scrubbed after successful re-parse)
- [ ] `warnings` does NOT contain any entry starting with `dropped_pictures` (scrubbed)
- [ ] `warnings` does NOT contain `docling_unavailable`
- [ ] `warnings` does NOT contain `docling_models_download_failed`
- [ ] `text` (the stitched markdown) contains at least one of the substrings: `$$`, `\\begin{align}`, `\\begin{equation}`, or visible inline math notation rendered correctly by docling (e.g. `\\(...\\)` style or unicode math symbols intact)
- [ ] `text` length is within ±25% of the pymupdf4llm-only baseline length (sanity check that stitching didn't drop or duplicate large sections)
- [ ] `pages` (total page count) matches the PDF's actual page count (~25 for arXiv:2409.13740 v3 — verify with `mupdf info` or PyMuPDF)
- [ ] Re-running with `DOCLING_DISABLE=1` produces `extractor == "pymupdf4llm"` and `docling_pages == []` (toggle works)

## How to run
```
mkdir -p /tmp/ultrasearch_eval_pdfs
[ -f /tmp/ultrasearch_eval_pdfs/paperqa2.pdf ] || \
  curl -fsSL -o /tmp/ultrasearch_eval_pdfs/paperqa2.pdf https://arxiv.org/pdf/2409.13740.pdf

python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py parse.py \
  --pdf /tmp/ultrasearch_eval_pdfs/paperqa2.pdf --json | tee /tmp/parse_result_01.json

# Toggle-off control
DOCLING_DISABLE=1 python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py parse.py \
  --pdf /tmp/ultrasearch_eval_pdfs/paperqa2.pdf --json | tee /tmp/parse_result_01_disabled.json
```

Optional sanity check on the math content:
```
python3 -c "
import json
d = json.load(open('/tmp/parse_result_01.json'))
print('extractor:', d['extractor'])
print('docling_pages:', d['docling_pages'])
print('pages re-parsed:', len(d['docling_pages']))
print('total pages:', d['pages'])
print('warnings:', d['warnings'])
print('has math markers:', any(m in d['text'] for m in ['\$\$', r'\begin{align}', r'\begin{equation}']))
"
```

## Notes
This fixture is the main happy-path validation for the docling fallback.
Most likely failure modes:

1. **`docling` not installed** — `_import_docling` returns `(None, error_msg)`,
   `_docling_parse_pages` returns `{}`, the stitch step finds nothing to
   replace, `docling_skipped == true`, and a warning `"docling_unavailable"`
   appears. The fixture fails. Fix: `pip install docling` in the ultrasearch
   environment (handled by `ensure_env.py` per Stage 2 plan §1.4).
2. **MPS crash on first model load** — docling auto-selects MPS on Apple
   Silicon. On M1 8GB or weaker GPUs, the first transformer load can OOM
   with `RuntimeError: MPS backend out of memory`. The Stage 2 plan §3.4.6
   specifies a CPU fallback: catch `RuntimeError`, set
   `pipeline_options.accelerator_options.device = "cpu"`, retry once. If the
   retry is not implemented, this fixture will fail on M1 8GB machines.
3. **Page off-by-one** — pymupdf4llm splits pages at `"\n-----\n"` producing
   0-based indices; docling exports with `page_no=` that is 1-based. If the
   stitcher forgets the `+1`, the wrong page gets replaced (text from page N
   is overwritten by docling's page N-1). Symptom: `text` length unchanged
   but content from the math sections is now duplicated elsewhere and
   missing from where it should be. Run a `grep -c` on a known math
   identifier to detect.
4. **Warning scrub too aggressive** — if the implementer strips ALL
   warnings, not just `mangled_visual_layout` and `dropped_pictures`, then
   other warnings (e.g. `pdf_password_protected`, `parse_error_recovered`)
   that should propagate get lost. Verify by reading the implementation:
   `warnings = [w for w in warnings if not w.startswith(("mangled_visual_layout",
   "dropped_pictures"))]` — anything else must survive.
5. **First-run cost** — docling downloads ~358 MB of models on first use.
   Allow up to 5 minutes for the first run on a clean environment. Subsequent
   runs are fast (<30s for this PDF).
