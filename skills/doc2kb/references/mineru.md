# doc2kb — heavy tier: MinerU 4 + Popo

Подробности opt-in тяжёлого тира (тиры MinerU, замеры движков на русских
страницах, page-patching, MinerU-Popo stage 2). Краткий обзор и точки входа —
в `SKILL.md`. Heavy-deps ставятся только явным действием
(`ensure_env.py --tier mineru`).

## Optional MinerU tier (opt-in)

The default lightweight tier covers text-layer PDFs well. For image-only
(scanned) PDFs, or text-layer PDFs that produce `mangled_visual_layout`
/ `dropped_pictures` warnings from pymupdf4llm, you can opt into
[opendatalab/MinerU](https://github.com/opendatalab/MinerU) 4, `standard`
tier: small layout/table models plus the MinerU2.5-Pro-2605 1.2B VLM. It is
intentionally **never** activated automatically — torch, llama.cpp and ~2 GB
of models must be installed by an explicit user action.

**One-time install:**

```bash
python3 <skill_dir>/scripts/ensure_env.py --tier mineru
```

This installs `mineru>=4.2.3,<4.3` into the tier's **own venv**,
`<state>/mineru/venv` (Python 3.13), then runs
`_mineru_driver.py setup`, which fetches the standard-tier models
(`MinerU-4_models_torch` + `MinerU2.5-Pro-2605-1.2B-GGUF`, ~2 GB) into
`<state>/mineru/models`. The driver points MinerU there through
`MINERU_MODEL_BASE_DIR` / `MINERU_HOME`, so neither weights nor MinerU's
`config.yaml` land in `~/.mineru`; `MINERU_TELEMETRY_DISABLED=1` is always
set. `extract_pdf_mineru.py` runs `<state>/mineru/venv/bin/python -I
_mineru_driver.py parse …`; nothing in the lightweight venv imports mineru.
Re-running `--tier mineru` is a no-op unless `requirements-mineru.txt`
changes. A venv that still holds MinerU 3.2 is upgraded in place (its
mlx / mlx-lm / mlx-vlm packages stay behind unused).

The tier used to share the lightweight venv, where MinerU 3's
`pypptx-with-oxml` (a python-pptx fork installing into the same `pptx`
package) overwrote python-pptx's files under `extract_pptx.py`; a shared
venv that still holds the tier is rebuilt once without it.

### Why MinerU 4 `standard` (measured 2026-11-20)

30 Russian pages, each rasterized to an image-only PDF at 200 DPI so no
engine could lean on a text layer: 8 pages of a scanned ГОСТ Р ИСО/МЭК
15408-2-2008 (ground truth = its hidden OCR layer, disputed words checked
by hand against the scan), 7 pages of a LaTeX lab report (tables, display
formulas; formula ground truth from the `.tex`), 8 Google-Slides lecture
slides (formulas as images, transcribed by hand), 7 LibreOffice Impress
slides (text, tables). Apple M5 Pro / 24 GB.

- *Cyrillic CER* — Levenshtein distance over the stream of Cyrillic words,
  prose pages only; table / multi-column / title pages count in the
  order-free *word F1* instead. Running headers, footers and page numbers
  are dropped on both sides; diagram transcriptions (mermaid) are not scored.
- *Tables* — the decimal numbers of the 3 table pages, in reading order.
- *Formulas* — 11 display formulas; canonicalized LaTeX similarity, ≥ 0.95
  counts as a match. *LaTeX valid* — every math span compiled with
  pdflatex (amsmath + mathtext).
- *s/page* — whole 30-page run ÷ 30, including per-document model setup;
  in brackets the first 1-page call (cold start).

| engine | Cyrillic CER (prose) | ГОСТ scan | lab report | ML slides | Impress slides | word F1 (30 p.) | table numbers recall / CER | formulas ≥ 0.95 / mean sim | LaTeX valid | s/page (1st call) |
|---|---|---|---|---|---|---|---|---|---|---|
| **MinerU 4.2.3 standard** (llama.cpp) | **0.20 %** | **0.07 %** | 0.26 % | 1.16 % | 0.11 % | **0.993** | 99.75 % / 0.18 % | 9/11 / 0.953 | 36/36 | **3.5** (12.9 s) |
| MinerU 3.2.3 vlm-auto-engine (MLX) | 0.27 % | 0.27 % | 0.18 % | 0.56 % | 0.11 % | 0.993 | 99.75 % / 0.18 % | 7/11 / 0.869 | 32/32 | 16.2 (32.6 s) |
| PaddleOCR-VL-1.6 (mlx-vlm 0.8 + PP-DocLayoutV3) | 0.70 % | 0.46 % | 0.31 % | 3.40 % | 0.19 % | 0.974 | 99.75 % / 0.18 % | **10/11 / 0.972** | 30/30 | 4.6 (3.4 s)¹ |
| docling 2.151 + ocrmac + CodeFormulaV2 | 8.30 % | 5.11 % | 1.80 % | 2.95 % | 31.73 % | 0.974 | 96.71 % / 1.15 % | 8/11 / 0.91 | 11/11 | 11.4 (84.2 s) |
| MinerU 4.2.3 basic (torch) | 100 % | 100 % | 100 % | 100 % | 100 % | 0.0 | 81.01 % / 18.81 % | 8/11 / 0.905 | 67/67 | 1.7 (10.8 s) |

¹ model preloaded by `mlx_vlm.server`, so its load time is not in the figure.

What the numbers hide:

- **MinerU 4 `basic` reads no Cyrillic at all** — its OCR has no Cyrillic
  recognizer and there is no OCR language setting; Russian text comes out
  as bare digits and punctuation. The wrapper defaults to `standard` and
  warns on `--tier basic`; the legacy `--backend pipeline` also runs
  `standard`.
- **PaddleOCR-VL-1.6** makes real Russian OCR errors on the scan
  ("Уничножение", "whichсюжет", "П3" for "ПЗ") and turned one table slide
  into a single image (60 words lost). Its formulas were the best. mlx-vlm
  0.8.0's continuous batching crashes on it (`broadcast_shapes (8) and
  (7)`) as soon as PaddleOCR sends blocks concurrently — it only ran with
  `--max-num-seqs 1`. Integration would add paddlepaddle + paddlex + a
  separate mlx-vlm server process.
- **docling** dropped whole lines on the scan and scrambled reading order
  on slides (word recall stays 97 %, CER does not); Cyrillic inside
  formulas becomes garbage (`в секундах` → `B \ c e k y {H}`).
- **MinerU 3.2.3** silently dropped 2 of 11 display formulas; both MinerU
  versions "normalize" a few words on the scan (предоставили →
  предоставляли) — the only engine errors left on that page set.
- **Every engine** turns Cyrillic inside math into Greek-looking LaTeX
  (`Гц` → `\Gamma \mathrm{u}`); check unit-bearing formulas by eye.
- All engines emit compilable LaTeX, so validity does not separate them;
  correctness does.

Published benchmarks disagree with these results for MinerU (MDPBench
Russian: 14.7 for MinerU-2.5-VLM vs 86.0 for PaddleOCR-VL-1.5,
arXiv 2603.28130) — they predate the MinerU2.5-Pro-2605 weights both
MinerU 3.2 and 4.2 now use.

### Tiers and flags

- `--tier standard` (default) — layout/table models + VLM; on Apple Silicon
  the small models run on MPS (torch) and the VLM through llama.cpp on
  Metal. No mlx packages are needed any more.
- `--tier advanced` — the same models with more inference compute.
- `--tier basic` — small models only. **Never for Cyrillic** (see above).
- `--ocr-mode ocr` (default) — the VLM reads every block. `auto` takes text
  from the PDF's text layer where one exists: on the LaTeX lab report that
  kept line-break hyphenation (`вычисля- лось`) and flattened a 6-column
  table to 2 columns — exactly the damage page-patching is meant to repair.
  `txt` = text layer only.
- Legacy 3.x flags are still accepted so old `_overrides.json` files and
  scripts keep working: `--backend vlm-auto-engine|hybrid-auto-engine|auto|pipeline`
  (all run `standard`, with a note in `warnings`), `--lang` (MinerU 4 has no
  OCR language setting; ignored with a warning), `--mineru-bin` (its
  sibling `python` is used). In `_overrides.json` write
  `"mineru": {"tier": "standard", "keep_raw": true}`.

MinerU 3.x tuning advice (`MINERU_PDF_RENDER_THREADS`,
`MINERU_PROCESSING_WINDOW_SIZE`, the mlx < 0.31.2 pin) no longer applies.

**Usage in scout:**

```bash
python3 <skill_dir>/scripts/ensure_env.py scout_corpus.py \
    <input_dir> <kb_dir> --enable-mineru
```

With the flag, `image_only` PDFs get `extraction_strategy: "mineru"`
instead of surfacing as an `ask_user_ocr_strategy` decision group. Text
PDFs continue going through pymupdf4llm. The flag choice is recorded in
`_scout.flags.enable_mineru`.

**Direct extraction:**

```bash
python3 <skill_dir>/scripts/ensure_env.py extract_pdf_mineru.py \
    "<absolute input>" "<kb_dir>/docs/<id>-<slug>.md" \
    --doc-id <id> --source-rel "<rel/path.pdf>" \
    [--tier standard|advanced] [--ocr-mode ocr|auto|txt] \
    [--keep-raw]    # cache raw mineru output for postprocess_popo.py
```

**Page-targeted patching (recommended for `dropped_pictures` follow-ups).**
When pymupdf4llm's `dropped_pictures` warning calls out a handful of
pages whose vector math/diagrams didn't survive, don't re-extract the
whole book — feed only those pages to mineru via `--pages` and let it
splice them directly into the existing markdown via `--patch-into`:

```bash
python3 <skill_dir>/scripts/ensure_env.py extract_pdf_mineru.py \
    "<absolute input.pdf>" "<unused output path>" \
    --doc-id <id> --source-rel "<rel/path.pdf>" \
    --pages "2,18-19,35,221,243-244,588" \
    --patch-into "<kb_dir>/docs/<existing-extraction>.md" \
    [--force-patch]    # only when target sha256 ≠ input sha256
```

What happens:
- The page list goes to MinerU's native `page_range`; only those pages
  are parsed, and MinerU keeps their original page indices, so nothing is
  sliced or remapped (≈3 s/page + ~10 s model setup on Apple-Silicon
  `standard`; 3 lab-report pages took 25 s end to end).
- `[page N]` anchors start at the line of each page's first heading,
  paragraph, equation, linked image or caption — figure-only pages get an
  anchor too.
- The target's `[page N]` sections for the listed pages are replaced
  in place; everything else is untouched.
- The target's frontmatter records `mineru_patched_pages: [...]` and
  `extraction_method_supplementary: mineru-<tier>@<version>` so the
  audit trail shows both extractors.
- Only images the new markdown links are copied (MinerU 4 also crops every
  equation and table into `images/`; those stay out of the kb). They carry
  an extra `-mineru-` infix (`<doc_id>-page<orig:03d>-mineru-imgN.ext`) so
  they never collide with pymupdf4llm's `<doc_id>-page<N>-imgN` filenames.

You can also run `--pages` *without* `--patch-into` to write a
standalone patch md (useful for review before splicing). The
`--patch-into` step then becomes a separate, idempotent invocation.

Refuse to splice if the target's `source_sha256` ≠ the input PDF's
sha256 (exit 1). Pass `--force-patch` to override — only do this when
the input PDF is a known re-export of the same document.

Only run mineru when pymupdf4llm warns about `dropped_pictures` or
`mangled_visual_layout`, or the PDF is a scan — for clean text-layer PDFs
it isn't worth the model setup. For a `dropped_pictures` warning that
names a few specific pages, prefer the `--pages … --patch-into …`
workflow above over re-extracting the whole document.

If the tier isn't installed the script exits 2 with the install hint —
the parent loop must treat that as "user action required", not as a
corrupt-PDF failure. `extraction_method` lands in frontmatter as
`mineru-<tier>@<version>` (3.x extractions say `mineru-<backend>@<version>`).

## Optional stage 2: MinerU-Popo post-processing (opt-in)

[opendatalab/MinerU-Popo](https://github.com/opendatalab/MinerU-Popo) is a
4B post-processing model that reconstructs document-level tree structure
(heading hierarchy, cross-page table merging, paragraph truncation
repair) from page-level OCR output. Use only when long-document
hierarchy still looks broken after MinerU — for short PDFs the gain is
negligible and the infra cost (separate conda env, 4B model download,
optional external LLM API for enrichment) isn't justified.

doc2kb ships only the *glue* — `postprocess_popo.py`. The Popo conda
env, the HF model download, and any `qwen_generate`/`gpt_generate`
configuration are handled by the user per the upstream Popo README.
Without the glue knowing where Popo lives the script exits 2 with
exact install instructions.

**Setup (one-time, by the user):**

```bash
git clone https://github.com/opendatalab/MinerU-Popo.git
cd MinerU-Popo
conda create -n popo python=3.10 && conda activate popo
pip install -r requirements.txt
hf download DreamEternal/MinerU-Popo --local-dir models/Mineru-Popo
export DOC2KB_POPO_REPO="$PWD"
```

`postprocess_popo.py` exports `POPO_MODEL_PATH=<repo>/models/Mineru-Popo`
and `POPO_INFERENCE_BACKEND=transformers` itself, so `model_utils.py` needs
no edit (an exported value of either wins). Without the bootstrap sentinel
it runs Popo with the `python3` on PATH — activate the env first.

**Usage:**

```bash
# First, run mineru with --keep-raw so the per-doc cache is preserved:
#   <kb_dir>/_mineru/<doc_id>/vlm/   MinerU 4 output + the Content List V1 as
#                                    <doc_id>_content_list.json (what Popo's
#                                    MinerU reader looks up)
#   <kb_dir>/_mineru/<doc_id>/<doc_id>.pdf   copy of the source PDF — Popo
#                                    renders its pages as the VLM's image input
python3 <skill_dir>/scripts/ensure_env.py extract_pdf_mineru.py \
    "<input.pdf>" "<kb_dir>/docs/<id>-<slug>.md" \
    --doc-id <id> --source-rel "<rel>" --keep-raw

# Then post-process. Writes <kb_dir>/docs/<id>-<slug>.tree.json per doc.
python3 <skill_dir>/scripts/ensure_env.py postprocess_popo.py <kb_dir> [--doc-id <id>]
```

How the glue drives Popo. Upstream's `scripts/*.sh` wrappers are **not**
used: `run_label_normalization.sh` passes `--pdf-dir eval_pdf_dir`, which
crashes when that dir is absent, and `run_inference.sh` caps the run at
`--limit 5` documents. Instead the glue calls Popo's Python entry points
with the env's python and explicit paths:

1. `post_processing/label_normalization.py --model mineru --input-dir <kb_dir>/_mineru --doc-id <ids…> --pdf-map-json …` —
   Popo's `MineruReader` reads `<input-dir>/<doc_id>/vlm/<doc_id>_{model,middle,content_list}.json`,
   which is the `_mineru/` cache layout as is, so nothing is copied into the
   Popo checkout. The pdf map points each doc at its cached `<doc_id>.pdf`;
   the PDF stem is also what Popo names every later output after.
2. `post_processing/run_inference.py --model mineru --limit 0` — transformers
   backend, `POPO_MAX_NEW_TOKENS=8192` (upstream's value).
3. `post_processing/get_json_tree.py`.

Inference and tree building take every `*.json` in a directory, so each
gets a fresh selection holding only this run's docs. Intermediate files
stay in the kb and are reused by `--skip-normalization` / `--skip-inference`:

```
<kb_dir>/_popo/
  pdf_map.json
  label_normalization/mineru/<doc_id>.json   # Popo's per-page block format
  inference/<doc_id>.json                    # blocks + contd/level/image labels
  build_tree/<doc_id>.json                   # the tree → docs/<id>-<slug>.tree.json
  build_tree_txt/<doc_id>.txt                # indented "title|preview" outline
```

Per-doc results (and why a doc got no tree) land in the success JSON and
`<kb_dir>/_logs/popo.json`; the run exits 1 only when no doc got a tree.
A cache from before MinerU 4 (no `vlm/<doc_id>_content_list.json`) or
without the PDF copy is reported per doc with "re-run
`extract_pdf_mineru.py --keep-raw`". `--popo-repo /abs/path` overrides
the env var. `--run-name` (it named a folder in the shared checkout) is
accepted and ignored.

### Auto-route MinerU → Popo (env-gated, default OFF)

Если хочется, чтобы **каждый** mineru-извлечённый документ автоматически
проходил Popo прямо из Phase 4 — выставьте env. По-умолчанию ничего не
меняется (heavy-deps-opt-in инвариант сохраняется):

| env | эффект |
|---|---|
| `DOC2KB_ALWAYS_POPO=1` | `extract_corpus.py` после каждого mineru-extract'а гоняет doc через Popo (форсит `--keep-raw`, чтобы кэш `_mineru/<id>/` существовал). Non-fatal: падение Popo не роняет файл из `extracted`, а surface'ится в `popo[]`/`needs_attention[]`. |
| `DOC2KB_POPO_AUTO=1` | разрешает `bootstrap_popo.py` сам поставить Popo (clone + env + **скачать ~16 ГБ модель**) при первом запросе, через `postprocess_popo.py --auto-setup`. |
| `DOC2KB_POPO_REPO` | (существующий) явный путь к checkout'у Popo; всё ещё работает, но теперь опционален. |

Per-file флаг `popo` в `_overrides.json` перекрывает env (`true`/`false`).
Два разных env специально: согласие на роутинг (`ALWAYS_POPO`) отделено от
согласия на multi-GB установку (`POPO_AUTO`) — скачивание модели никогда не
происходит молча. Оба вместе = полностью автоматический режим.

### Auto-bootstrap Popo (`bootstrap_popo.py`, opt-in)

Вместо ручного clone/conda/hf-download из README выше:

```bash
python3 <skill_dir>/scripts/ensure_env.py bootstrap_popo.py [--skip-model] [--force-model]
```

Клонирует Popo (в `$DOC2KB_POPO_REPO` или `<state-dir>/popo/MinerU-Popo`),
создаёт отдельный env (приоритет **uv → conda → venv**, Python 3.10),
ставит зависимости платформо-зависимо, скачивает 4B-модель и патчит путь к
ней. Python этого env записан в sentinel `.doc2kb-popo-python`;
`postprocess_popo.py` запускает им точки входа Popo напрямую (без
`conda activate`), так что uv-venv работает.

> **macOS / Apple Silicon:** апстрим-`requirements.txt` у Popo — это тяжёлый
> CUDA/vLLM **серверный** стек (`torch+cu12`, `nvidia-*`, `cupy`, `triton`) и на
> Mac не встанет. Но doc2kb гоняет Popo через его **transformers**-бэкенд
> (`postprocess_popo.py` ставит `POPO_INFERENCE_BACKEND=transformers`), которому
> ничего из этого не нужно. Реальная import-поверхность Popo — `torch`,
> `transformers`, `PIL`, `fitz`, `bs4`, `openai`, `requests`, `tqdm` (+
> `accelerate`/`qwen-vl-utils`), всё это ставится из `requirements-popo-mac.txt`.
> Модель — Qwen3-VL **4.44B** (~16 ГБ fp32 на диске / ~9 ГБ bf16 в RAM).
> **Проверено на Apple Silicon:** грузится целиком на **MPS** и держит **~22
> tok/s** — но только если `device_map` запинен на MPS. Стоковый
> `device_map="auto"` у Popo на Mac сбрасывает часть весов на **диск** (accelerate
> ошибается в оценке памяти MPS) → медленно и пустой вывод; поэтому
> `bootstrap_popo.py` на darwin патчит `"auto"→{"":"mps"}`. Путь к модели
> прокидывается через `POPO_MODEL_PATH` (его выставляет `postprocess_popo.py`).
> `PYTORCH_ENABLE_MPS_FALLBACK=1` ставится автоматически — неподдержанные
> MPS-операции уходят на CPU. Прикидка: один Popo-проход ≈ 1–5 мин/документ. На
> CUDA-Linux bootstrap ставит полный апстрим-`requirements.txt`.
