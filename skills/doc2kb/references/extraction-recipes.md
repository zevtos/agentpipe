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
    [--timeout 600] [--normalize] [--quiet] [--workers 4] [--embed-depth 4]
```

Summary shape (last stdout line):

```json
{
  "ok": true,
  "counts": {"extracted": N, "unchanged": N, "skipped_by_decision": N,
             "error": N, "needs_attention": N},
  "extracted_but_flagged": N,
  "embedded_documents": N,          // child docs for embedded objects (format-spec «Embedded documents»)
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
the venv on first call (global state dir, ADR-008) and execs the target script
through the venv python:

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
| `xml`               | `extract_xml.py`                | `<input> <output> --doc-id <id> --source-rel <rel>` (lxml parse, `recover=True`, entity resolution off. XHTML root → the HTML route: trafilatura → markdownify fallback. Generic XML → text blocks in document order, tags/attributes stripped with a warning; attribute-value salvage pass when the text yield is tiny. Malformed XML → raw-text passthrough, never fails) |
| `xlsx`              | `extract_xlsx.py`               | `<input> <output> --doc-id <id> --source-rel <rel>` [`--max-sheets 100`] [`--max-rows 10000`] [`--max-cells 300000`] [`--timeout 20`] [`--max-rss-mb 1024`] (openpyxl `read_only=True, data_only=True, keep_links=False`, parsing through defusedxml — refuses to run without it — in a child process killed past the wall deadline or the RSS ceiling → `"refused: resource: time|memory"` — the child also enforces both itself and exits (removing its temp dir) the moment its parent dies, so a caller's own timeout kill leaves no orphan; env `DOC2KB_XLSX_TIMEOUT` / `DOC2KB_XLSX_MAX_RSS_MB`; the result carries `peak_rss_mib`. Pre-check first → `{"ok": false, "refused": true}`: zip bomb (members, per-member/total/sharedStrings size, ratio, encryption), any `<!DOCTYPE`/`<!ENTITY` in any member whatever its name, non-UTF-8 XML, a row past 16384 cells, a text run past 256 KiB. Wider than 64 columns → sparse `row N: A=…` lines. Past a sheet/row/cell/scan/time/8 MiB-output cap, or a cell cut at 2000 chars, the body gets a `> **[truncated]**` line and the JSON `"truncated": true, "truncation": ["<kind>: …"]`, kind one of sheets/rows/cells/scan/time/output/cell_chars; rows numbered at or below an earlier row are kept in file order and listed in `"row_order"` / a `row_order:` warning) |
| `ipynb`             | `extract_ipynb.py`              | `<input> <output> --doc-id <id> --source-rel <rel>` |
| `epub`              | `extract_epub.py`               | `<input> <output> --doc-id <id> --source-rel <rel>` (pandoc when on PATH — honours OPF spine order, preserves tables/images, converts EPUB3 MathML → `$...$` LaTeX; else pure-Python `zip`+`markdownify` → images become `![image N]()` placeholders and MathML is dropped. Force the fallback with `--force-fallback`. Auto image extraction to `<kb_dir>/assets/`; same `--assets-dir`/`--assets-rel`/`--no-extract-images` flags) |
| `skip`              | (none — file is skipped) | — |
| `needs_password`    | (Phase 3 user decision; if password given, re-classify and use `pymupdf4llm`) | — |
| `needs_ocr_or_vlm`  | (Phase 3 user decision; pick `vlm_mlx` to route through `mineru` strategy — requires opt-in tier; default behaviour is `skip`) | — |
| `not_in_mvp`        | (XLSX/ODT/image — Phase 3 user decision; skip in MVP) | — |

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

## Auto-recovery (built into the extractors)

Эти проходы происходят автоматически внутри extract-скриптов — вмешательство
агента не требуется. Сюда заглядывают, когда warning выглядит незнакомо или
нужно подкрутить поведение.

### DOCX с математикой — pandoc-маршрут

Если scout пометил DOCX как `has_equations: true` и `pandoc` есть на `PATH`,
`extract_docx.py` автоматически переключается с mammoth на pandoc — он сохраняет
OOXML math (`<m:oMath>`) как `$...$`/`$$...$$` LaTeX. Mammoth по-тихому дропает
math-элементы, и body после него ссылается на «формулу (1)», у которой нет
содержимого. JSON-поле `extractor` сообщает, какой маршрут был использован
(`pandoc` или `mammoth+markdownify`). Если pandoc недоступен на машине с
math-документом — warning с инструкцией установить (`brew install pandoc` /
`apt install pandoc`).

### Поломанные лигатуры `fi` / `ff` / `fl` (PDF)

pymupdf4llm ≤ 1.27.x теряет одну букву из ASCII-смаппленных лигатур в его
spans→markdown сборке: `Ofcial` вместо `Official`, `fexible` вместо `flexible`,
`trafc` вместо `traffic`, `Diffculty`, `quantifers`, и т.д. Raw
`pymupdf.Page.get_text` отдаёт буквы корректно — баг локален в pymupdf4llm.
`extract_pdf_pymupdf4llm.py` автоматически прогоняет `recover_ligatures` из
`_common.py` на body и эмитит warning `ligatures_recovered: N word(s) ...`.

- Идемпотентно (повторный вызов даёт 0 правок); регистр первой буквы
  сохраняется (`Ofcial` → `Official`, `ofcial` → `official`).
- Незнакомый broken pattern → дополнительный warning `ligature_residual: ...`
  с sample. Расширьте `_LIGATURE_FIXES` в `scripts/_common.py`.
- Безопасность: lookbehind `(?<![A-Za-z])` срабатывает и когда broken-слово
  обёрнуто в markdown italic (`_fnd_` → `_find_`); lookahead отказывается над
  legit-префиксами (`different` не превращается в `diffierent`).

### Footer-номера страниц (PDF)

PDF-страницы часто кончаются голым номером страницы перед маркером следующей
(`...текст...\n\n1\n\n[page 2]`). `detect_recurring_lines` из `normalize_md` их
не ловит (каждый номер уникален: 1, 2, …, N). `strip_page_footer_numbers` из
`_common.py` ловит позиционно: standalone-число между двумя `[page N]` маркерами
или в самом конце тела. Маркеры `[page N]` сохраняются — они нужны второму
агенту для навигации. Вызывается из `extract_pdf_pymupdf4llm.py` сразу после
`recover_ligatures`, count идёт в stderr (без warning — поведение всегда
корректное).

### Встроенные картинки → `assets/` (PDF)

Когда pymupdf4llm эмитит `==> picture [WxH] intentionally omitted <==`
плейсхолдеры (формулы, матрицы, диаграммы, нарисованные картинками),
`extract_pdf_pymupdf4llm.py` автоматически:

1. Извлекает встроенные изображения через `pymupdf` в `<kb_dir>/assets/`.
2. Заменяет плейсхолдеры на Markdown-ссылки
   `![page N, image M](../assets/<doc_id>-pageNN-imgM.<ext>)`.
3. Подавляет `dropped_pictures` warning для заменённых плейсхолдеров —
   warning остаётся только на те, где картинки нет в PDF-stream (редко).

Дефолтное место — `<output_md>.parent.parent / "assets"` (= стандартный layout
`<kb_dir>/docs/*.md` → `<kb_dir>/assets/<file>`). Override: `--assets-dir <abs>`
и `--assets-rel <prefix>`. Отключить: `--no-extract-images` (вернёт исходное
поведение с loud warning).

Если Pillow не декодирует картинку из PDF (JBIG2, CMYK JPX), экстрактор
перекодирует её через MuPDF (`pymupdf.Pixmap`) в PNG, а не пропускает.

### Векторные картинки (EMF/WMF/SVG/PDF/EPS) → PNG (все экстракторы)

Схемы в Office часто лежат метафайлами: вставка из Visio, «векторный
клипарт», превью старых формул. EPUB несёт SVG, DOCX иногда — PDF или EPS.
`save_image_safe()` (общая точка для всех экстракторов, `_raster.py`)
распознаёт их по сигнатуре, а не по расширению (python-pptx, например,
называет EMF `wmf`), и растеризует в PNG:

| формат | чем | если нет инструмента |
|---|---|---|
| EMF, WMF | LibreOffice `soffice --headless --convert-to pdf` (одним запуском на документ, отдельный `-env:UserInstallation` во временном каталоге) → PyMuPDF | пропуск + warning `vector image (emf) not rasterized — LibreOffice is not on PATH — install it …` |
| SVG | PyMuPDF (внешние ссылки вырезаются, `<!ENTITY` — отказ) | — |
| PDF-картинка | PyMuPDF, страница 1 | — |
| EPS | Ghostscript `gs -dSAFER … -dEPSCrop` → PDF → PyMuPDF; иначе TIFF/WMF-превью DOS-EPS | пропуск + warning с `brew install ghostscript` |

Отрендеренная страница обрезается по нарисованному (LibreOffice кладёт
метафайл на лист A4) и масштабируется до 1600 px по длинной стороне. Файл в
`assets/` получает расширение `.png`. LibreOffice и Ghostscript — системные
инструменты, как конвертер `.doc`: в venv не ставятся, pip-зависимостей не
добавляется. 10 метафайлов лекции ≈ 2 с.

### Вложенные документы (рекурсивно, по умолчанию)

DOCX/DOC/PPTX/XLSX/PDF-экстракторы после своего тела разбирают вложения:
OLE-объекты и встроенные файлы (`*/embeddings/*`), workbook с данными
диаграммы, вложения PDF. Каждое вложение извлекается тем же экстрактором,
что и файл корпуса того же типа, в отдельный `docs/<id>_eNN-<slug>.md`, и
так до глубины 4 (`--embed-depth N` у `extract_corpus.py`/`dkb`, env
`DOC2KB_EMBED_DEPTH`; `0` выключает). В родителе — ссылка на месте объекта
(PPTX) или в разделе `## Embedded objects`. Лимиты, формат и провенанс —
`format-spec.md` («Embedded documents»). Всё, что не извлечено (глубина,
неподдерживаемый тип вроде `.xls`, лимит размера, zip-бомба), видно в
warnings родителя и поднимается до файла корпуса.

### PPTX: диаграммы, SmartArt, группы

- Диаграмма → строка `**Chart: <title>** (<type>)` и таблица категории ×
  серии из закэшированных в XML значений (раньше — `*(chart)*`).
- SmartArt → `*(SmartArt)*` и список текстов узлов (python-pptx SmartArt
  не видит, раньше диаграмма пропадала целиком).
- Группы обходятся на любую глубину (раньше — один уровень; текст во
  вложенных группах терялся молча).
