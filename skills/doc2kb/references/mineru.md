# doc2kb — heavy tier: MinerU VLM + Popo

Подробности opt-in тяжёлого тира (бэкенды, бенчмарки, тюнинг, page-patching,
MinerU-Popo stage 2). Краткий обзор и точки входа — в `SKILL.md`. Heavy-deps
ставятся только явным действием (`ensure_env.py --tier mineru`).

## Optional MinerU VLM backend (opt-in)

The default lightweight tier covers text-layer PDFs well. For image-only
(scanned) PDFs, or text-layer PDFs that produce `mangled_visual_layout`
/ `dropped_pictures` warnings from pymupdf4llm, you can opt into the
[opendatalab/MinerU](https://github.com/opendatalab/MinerU) VLM-grade
extractor. It is intentionally **never** activated automatically — heavy
ML deps (~3 GB model + MLX wheels on macOS) must be installed by an
explicit user action.

**One-time install:**

```bash
python3 <skill_dir>/scripts/ensure_env.py --tier mineru
```

This adds `mineru[all]` plus (on Apple Silicon) `mlx-vlm`, `mlx`, and
`mlx-lm` into the same venv as the lightweight base. A separate hash
file (`<venv>/.installed_hash_mineru`) keeps the install idempotent —
re-running `--tier mineru` is a no-op unless `requirements-mineru.txt`
changes. The `mlx-vlm` pin matters: mineru's auto-engine selector
(`mineru/utils/engine_utils.py::_select_mac_engine`) only picks the
fast MLX backend when `mlx-vlm` is importable; without it, mineru
silently falls back to the much slower transformers path.

**Apple Silicon tuning (M-series).** With the mineru tier installed,
mineru auto-detects MLX. The official tuning knobs (`MINERU_PDF_RENDER_THREADS`,
`MINERU_PROCESSING_WINDOW_SIZE`, `MINERU_FORMULA_ENABLE`,
`MINERU_TABLE_ENABLE`) target long-document throughput on multi-GPU
serving setups. **Measured on M5 Pro / 24 GB**, lab2_advanced.pdf (10 p):
setting `MINERU_PDF_RENDER_THREADS=8` and `MINERU_PROCESSING_WINDOW_SIZE=128`
made the same vlm-auto-engine run go from ~65 s to ~207 s with bit-for-bit
identical output. The likely cause: render-stage threads contend with
MLX for unified-memory bandwidth, and the larger window adds batch-setup
overhead a 10-page document never recoups.

Recommendation: **don't set these env vars globally on a laptop class
M-series machine**. If you ever process a long book/dissertation (100+ p)
and want to experiment, set them per-invocation and measure — don't
trust the upstream docs blindly here. For everything else, leave
mineru's own defaults alone; `MINERU_FORMULA_ENABLE=false` /
`MINERU_TABLE_ENABLE=false` are the only knobs worth flipping when you
know your corpus is pure prose and want to shave VLM calls.

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
    [--backend vlm-auto-engine|hybrid-auto-engine|pipeline] \
    [--lang cyrillic|en|ch|...] \
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
    [--lang cyrillic|en|ch|...] \
    [--backend vlm-auto-engine|hybrid-auto-engine|pipeline] \
    [--force-patch]    # only when target sha256 ≠ input sha256
```

What happens:
- The script slices the input PDF down to just the listed pages with
  pymupdf in a tempdir.
- mineru runs only on the subset (≈10 s/page on Apple-Silicon vlm-mlx,
  vs. ≈2 hours for a 600-page book).
- Page anchors and asset filenames are remapped to the original page
  numbers — the splice writes `[page 243]` and
  `<kb_dir>/assets/<doc_id>-page243-mineru-imgM.<ext>`, never the
  internal subset indices.
- The target's `[page N]` sections for the listed pages are replaced
  in place; everything else is untouched.
- The target's frontmatter records `mineru_patched_pages: [...]` and
  `extraction_method_supplementary: mineru-<backend>@<version>` so
  the audit trail shows both extractors.
- mineru's assets carry an extra `-mineru-` infix
  (`<doc_id>-page<orig:03d>-mineru-imgN.ext`) so they never collide
  with pymupdf4llm's existing `<doc_id>-page<N>-imgN` filenames.

You can also run `--pages` *without* `--patch-into` to write a
standalone patch md (useful for review before splicing). The
`--patch-into` step then becomes a separate, idempotent invocation.

Refuse to splice if the target's `source_sha256` ≠ the input PDF's
sha256 (exit 1). Pass `--force-patch` to override — only do this when
the input PDF is a known re-export of the same document.

Backend trade-off (measured back-to-back on M5 Pro / 24 GB,
lab2_advanced.pdf 10 p, math-heavy):
- `vlm-auto-engine` (default) — pure VLM end-to-end via MLX. **206 s**
  on the sample doc, produces clean `$X_{sp}$` LaTeX, recovered three
  state-space matrices and the PixHawk block diagram as Mermaid.
- `hybrid-auto-engine` — pipeline does layout, VLM does crops. Mineru's
  own CLI default. **243 s** on the same doc (~18% slower than vlm on
  M-series); LaTeX subscripts come out as `$X _ { s p } ,$` with extra
  spaces and occasional trailing-punct adhesion. Reportedly 2-3× faster
  than VLM on CUDA Linux without MLX — flip the default there.
- `pipeline` — CPU/GPU CV stack, no VLM, no big model download.
  Fastest, least accurate. Right choice on CPU-only boxes or when you
  just need a structural pass.

VLM inference is the bottleneck regardless of backend on M-series.
Reference numbers from community + own benchmarks:

| Hardware | vlm-mlx (s/page) | pipeline (s/page) | source |
|---|---|---|---|
| M2 Max (~38 GPU cores, 64+ GB) | ~0.3 | ~0.9 | community |
| M5 Pro (≈16 GPU cores, 24 GB) | ~20 | not measured | own |
| Mac mini M4 (10 GPU cores, 16 GB) | ~38 | ~32 | community |

So a 50-page lecture takes ~15 minutes on M-series "pro" laptops, and
upper-tier desktop chips (M2 Max +) blow past that by an order of
magnitude thanks to wider GPU/memory pipelines. On RAM-constrained
M-series (mac mini class), `--backend pipeline` is actually competitive
with vlm-mlx on speed and can be the right call for prose-heavy
corpora.

Only run mineru when pymupdf4llm warns about `dropped_pictures` or
`mangled_visual_layout` — for clean text-layer PDFs it isn't worth the
minutes-per-document cost. For a `dropped_pictures` warning that names
a few specific pages, prefer the `--pages … --patch-into …` workflow
above over re-extracting the whole document.

If the `mineru` CLI isn't on PATH the script exits 2 with the install
hint above — the parent loop must treat that as "user action required",
not as a corrupt-PDF failure. `extraction_method` lands in frontmatter as
`mineru-<resolved_backend>@<version>` so the audit trail distinguishes
which backend actually ran (MinerU may resolve `auto` to `vlm` or
`pipeline` depending on local hardware).

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
# Edit post_processing/model_utils.py to point POPO_MODEL_PATH at the
# downloaded model. Optionally configure qwen_generate/gpt_generate.
export DOC2KB_POPO_REPO="$PWD"
```

**Usage:**

```bash
# First, run mineru with --keep-raw so the per-doc cache is preserved
# under <kb_dir>/_mineru/<doc_id>/.
python3 <skill_dir>/scripts/ensure_env.py extract_pdf_mineru.py \
    "<input.pdf>" "<kb_dir>/docs/<id>-<slug>.md" \
    --doc-id <id> --source-rel "<rel>" --keep-raw

# Then post-process. Reads <kb_dir>/_mineru/, runs Popo's 3 bash scripts
# (normalize → inference → build_tree), writes
# <kb_dir>/docs/<id>-<slug>.tree.json sidecars for each doc.
python3 <skill_dir>/scripts/ensure_env.py postprocess_popo.py <kb_dir>
```

Pass `--doc-id <id>` to process a single doc, `--popo-repo /abs/path`
instead of the env var, or `--skip-normalization` / `--skip-inference`
to iterate without redoing earlier steps.

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
ней. Popo'вские bash-скрипты зовут `python3` из PATH (без `conda activate`),
поэтому `postprocess_popo.py` подкладывает bin/ этого env в PATH через
sentinel `.doc2kb-popo-python` — uv-venv работает.

> **macOS / Apple Silicon:** апстрим-`requirements.txt` у Popo — это тяжёлый
> CUDA/vLLM **серверный** стек (`torch+cu12`, `nvidia-*`, `cupy`, `triton`) и на
> Mac не встанет. Но doc2kb гоняет Popo через его **transformers**-бэкенд
> (`run_inference.sh` ставит `POPO_INFERENCE_BACKEND=transformers`), которому
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
