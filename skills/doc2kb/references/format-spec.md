# doc2kb — Output Format Specification

This document is the source of truth for every artefact `doc2kb` produces.
Scripts read and write to these schemas; downstream agents read them in the
second session.

Contents: [File layout](#file-layout) · [`_scout.json`](#_scoutjson) ·
[doc frontmatter](#docsid-slugmd-frontmatter) · [Markdown body](#markdown-body) ·
[Embedded documents](#embedded-documents) ·
[Sharded documents](#sharded-documents-folder-form) · [`manifest.json`](#manifestjson) ·
[`INDEX.md`](#indexmd) · [`llms.txt`](#llmstxt) · [`AGENTS.md`](#agentsmd) ·
[extractor stdout JSON](#stdout-json-from-each-extract_py) ·
[dispatcher stdout JSON](#stdout-json-from-extract_corpuspy-phase-4-dispatcher)

## File layout

```
<kb_dir>/
├── manifest.json
├── INDEX.md
├── llms.txt
├── AGENTS.md
├── _scout.json            # scout output, kept for debugging
├── _logs/
│   └── errors.json        # extraction failures, if any
├── _media/<doc-id>/       # audio/video speech-to-text drafts: review.md (disputed spots), whisper.srt/.json,
│                          #   gigaam.txt, mlx.txt, run.log, asr.json — only for docs that went through ASR
├── _mineru/<doc-id>/      # opt-in (extract_pdf_mineru.py --keep-raw): MinerU output in vlm/ + <doc-id>.pdf
├── _popo/                 # opt-in (postprocess_popo.py): MinerU-Popo work files, see references/mineru.md
├── assets/                # extracted images: <doc-id>-pageNN-imgM.<ext>, <doc-id>-slideNN-imgM.<ext>,
│                          #   <doc-id>-imgNNN.<ext>; vector images (EMF/WMF/SVG/PDF/EPS) as .png
└── docs/
    ├── <doc-id>-<slug>.md
    ├── <doc-id>_e01-<slug>.md         # document embedded in <doc-id> (see "Embedded documents")
    ├── <doc-id>_e01_e02-<slug>.md     # …and one embedded in that one
    ├── <doc-id>-<slug>.tree.json      # opt-in MinerU-Popo document tree (postprocess_popo.py)
    └── ...
```

Filenames: `<doc-id>` is `doc-NNN` (zero-padded, scout-assigned). `<slug>` is
ASCII-normalized `Path(source).stem` lowercased (cyrillic and CJK chars are
dropped). Maximum length 48 chars after the prefix. An embedded document's id
appends `_eNN` (the object's position in its parent) to the parent id; `_` never
occurs in a scout id or a slug, so `<doc-id>_e*` matches exactly one parent's
descendants.

## `_scout.json`

Emitted by `scout_corpus.py`. Schema:

```json
{
  "schema_version": "1.0",
  "scout_tool": "doc2kb@<VERSION>",
  "scanned_at": "ISO-8601 UTC",
  "input_root": "/abs/path/to/input",
  "kb_root": "/abs/path/to/kb",
  "flags": {
    "enable_mineru": bool                  // opt-in heavy tier — image_only
                                           // PDFs auto-route to `mineru`
                                           // strategy when true
  },
  "corpus": {
    "total_files": int,
    "total_size_bytes": int,
    "estimated_tokens": int,
    "estimated_extraction_seconds": int,
    "scout_elapsed_seconds": float
  },
  "files": [
    {
      "id": "doc-NNN",                       // stable, used as kb-slug prefix
      "source_path": "rel/to/input_root",
      "sha256": "hex",                       // doubles as cache key
      "size_bytes": int,
      "mime": "application/pdf" | null,
      "mime_confidence": "high" | "low",     // low if magic ↔ ext disagree
      "source_type": "pdf"|"docx"|"doc"|"pptx"|"xlsx"|"md"|"txt"|"html"|"xml"|"ipynb"|"epub"|"rtf"|"audio"|"video"|"subtitles"|"odt"|"image"|"unknown",
      "pdf_class": "text"|"image_only"|"mixed"|"encrypted"|"corrupt" | null,
      "pages": int | null,
      "slides": int | null,
      "sheets": int | null,                  // xlsx: worksheet parts
      "xlsx_xml_bytes": int | null,          // xlsx: uncompressed sheet + shared-string XML (token proxy)
      "has_notes": bool | null,              // pptx
      "notes_chars": int,
      "inline_images": int,
      "has_tables": bool,
      "has_equations": bool,
      "cells": int | null,                   // ipynb
      "code_cells": int | null,              // ipynb
      "markdown_cells": int | null,          // ipynb
      "raw_cells": int | null,               // ipynb
      "has_outputs": bool | null,            // ipynb
      "embedded_objects": int,               // docx/pptx/xlsx/pdf: objects Phase 4 will look into
                                             // (OOXML */embeddings/* members, PDF attachments)
      "encoding": "utf-8" | "cp1251" | ... | null,
      "media_class": "ok"|"no_audio"|"corrupt"|"unprobed" | null,   // audio/video; unprobed = no ffprobe
      "media_duration_seconds": int | null,  // audio/video (ffprobe)
      "media_format": "mov,mp4,m4a,3gp,3g2,mj2" | null,  // ffprobe format_name
      "audio_streams": int | null,           // audio/video
      "video_streams": int | null,           // real picture streams (cover art / stills excluded)
      "subtitle_tracks": int | null,         // text subtitle streams inside the container
      "bitmap_subtitles": int | null,        // PGS/VobSub/DVB — not extracted (need OCR)
      "chapters": int | null,                // container chapters (become ## sections)
      "subtitle_sidecars": ["rel/lecture.ru.srt", ...],  // media: subtitle files paired by name (optional)
      "sidecar_of": "doc-NNN",               // subtitles: paired into that media doc (strategy skip; optional)
      "media": {"lang": "ru", "engines": "whisper,gigaam", "subtitles": "prefer"|"ignore"},  // optional, apply_overrides
      "extraction_strategy": "pymupdf4llm"|"mineru"|"mammoth"|"doc"|"rtf"|"python-pptx"|"passthrough-md"|"passthrough-txt"|"trafilatura"|"xml"|"xlsx"|"ipynb"|"epub"|"media"|"subtitles"|"needs_password"|"needs_ocr_or_vlm"|"not_in_mvp"|"skip",
      "estimated_tokens": int | null,
      "warnings": [string],
      "action_required": "ask_user_password_or_skip"|"ask_user_ocr_strategy"|"ask_user_proceed_huge"|"ask_user_skip_corrupt"|"ask_user_skip_unsupported" | null
    }
  ],
  "skipped_at_scout": [
    { "source_path": "rel", "reason": "..." }
  ],
  "user_decisions_needed": [
    {
      "type": "encrypted"|"scanned_pdf"|"huge_file"|"corrupt"|"unsupported_format",
      "files": ["rel/path", ...],
      "options": ["skip", ...],
      "default": "skip"
    }
  ]
}
```

## `docs/<id>-<slug>.md` frontmatter

Every extracted document has a YAML frontmatter block. Required fields:

| key                 | type        | source / meaning |
|---------------------|-------------|------------------|
| `id`                | string      | scout-assigned doc-NNN |
| `source`            | string      | relative path inside input corpus |
| `source_type`       | string      | `pdf`/`docx`/`doc`/`pptx`/`xlsx`/`ipynb`/`rtf`/`epub`/`md`/`txt`/`html`/`xml`/`audio`/`video`/`subtitles` |
| `source_sha256`     | string      | sha256 of original bytes |
| `extraction_method` | string      | `name@version` of the extractor used |
| `extraction_date`   | string      | YYYY-MM-DD UTC |
| `tokens_estimated`  | int         | tiktoken cl100k_base count of body |
| `warnings`          | list[str]   | any extraction-time issues |

Per-type optional fields:

| key                | applies to | meaning |
|--------------------|------------|---------|
| `pages`            | pdf        | total page count |
| `slides`           | pptx       | total slide count |
| `has_notes`        | pptx       | bool — speaker notes present |
| `notes_chars`      | pptx       | total chars in notes |
| `inline_images`    | docx/doc/pptx | count of embedded pictures |
| `has_tables`       | docx/doc/pptx | bool — at least one table |
| `has_equations`    | docx/doc   | bool — OOXML `<m:oMath>` present (`.doc` after converter→`.docx`) |
| `has_charts`       | pptx       | bool — chart shapes present |
| `sheets`           | xlsx       | worksheets in the workbook |
| `rows`             | xlsx       | non-empty rows written |
| `truncated`        | xlsx       | bool — a sheet/row/cell/scan/time/output cap cut the body, or a cell was cut at 2000 characters (each cut is marked `> **[truncated]** …` in the body and listed in `warnings` as `truncated: <kind>: …`) |
| `has_tracked_changes` | docx/doc | bool — `w:ins`/`w:del` present |
| `paragraphs`       | docx/doc   | total paragraph count |
| `source_encoding`  | md/txt/html/xml | detected source encoding |
| `cells`            | ipynb      | total cell count |
| `code_cells`       | ipynb      | code-cell count |
| `markdown_cells`   | ipynb      | markdown-cell count |
| `raw_cells`        | ipynb      | raw-cell count |
| `has_outputs`      | ipynb      | bool — any code cell has non-empty `outputs[]` |
| `language`         | ipynb      | language sniffed from `kernelspec`/`language_info`, allowlist-clamped (default `python`) |
| `kernelspec_name`  | ipynb      | original kernel name (optional, sanitized) |
| `assets`           | pdf/docx/doc/pptx/rtf/epub | list of relative paths to images extracted into `<kb_dir>/assets/` and referenced from the body (only emitted when at least one image was recovered). Vector images (EMF/WMF/SVG/PDF/EPS) are rasterized to `.png` |
| `embedded_objects` | docx/doc/pptx/xlsx/pdf | number of embedded objects found (OLE objects, embedded files, chart-data workbooks, PDF attachments); only when > 0 |
| `embedded_docs`    | docx/doc/pptx/xlsx/pdf | ids of the child docs extracted from them (`[doc-001_e01, …]`) |
| `duration_seconds` | audio/video/subtitles | length of the recording (ffprobe) or of the last cue; seconds only — an unquoted `mm:ss` reads as a base-60 int in YAML 1.1 |
| `transcript_source`| audio/video | `subtitles` (a sidecar file or a text track transcribed the speech) or `asr` (speech-to-text tier) |
| `subtitle_track`   | audio/video | which subtitles gave the text (`sidecar lecture.ru.srt`, `stream 2 (subrip, en)`) |
| `subtitle_sources` | audio/video | subtitle candidates found (sidecars + text tracks) |
| `sidecars_appended`| audio/video | paired subtitle files not used as the main text, appended as `## Subtitles: <file>` sections |
| `asr_engines`      | audio/video | engines in merge order: base, filler (`[whisper, gigaam]`) |
| `asr_words` / `asr_seconds` | audio/video | words in the merged transcript / wall time of the tier worker |
| `asr_review` / `asr_review_spots` | audio/video | kb-relative `_media/<id>/review.md` and its spot count (engine disagreements, loop/gap fills) |
| `language`         | audio/video/subtitles | spoken (detected or forced) or subtitle language, ISO 639-1 |
| `media_format` / `audio_codec` / `video_codec` / `resolution` / `audio_streams` / `chapters` / `media_title` | audio/video | container and stream facts from ffprobe |
| `subtitle_format` / `cues` / `source_encoding` | subtitles | parser used (`srt`/`vtt`/`ass`/`sbv`/`lrc`), cue count, detected encoding |
| `embedded_in`      | embedded doc | id of the parent doc |
| `embedded_member`  | embedded doc | where the object lives inside the parent (`ppt/embeddings/oleObject1.bin`, `EmbeddedFiles/x.pdf`, `page3/attachment1/x.pdf`) |
| `embedded_name`    | embedded doc | the object's file name (an OLE Package's stored name when there is one) |
| `embedded_location`| embedded doc | `slide 5`, `slide 7, chart data`, `page 3`, `document body`, `sheet1`, `attachment`, … when known |
| `embedded_depth`   | embedded doc | 1 for an object in a corpus file, 2 for an object inside that, … |
| `parent_source_sha256` | embedded doc | sha256 of the parent's bytes (the parent's own `source_sha256`) |
| `headings`         | all        | first up to 10 top-level headings, for fast index |
| `mineru_subset_pages` | pdf     | sorted list of original page numbers that mineru extracted, when the file was produced by `extract_pdf_mineru.py --pages` in standalone (non-splice) mode |
| `mineru_patched_pages` | pdf    | sorted union of every page that has been replaced via `extract_pdf_mineru.py --patch-into`; accumulates across runs against the same target md |
| `extraction_method_supplementary` | pdf | `mineru-<tier>@<version>` of the most recent mineru patch (3.x runs wrote `mineru-<backend>@<version>`); set alongside `mineru_patched_pages`, never replaces the primary `extraction_method` |

`extraction_method` values that callers should recognise:

- `pymupdf4llm@<ver>` — default PDF extractor.
- `mineru-<tier>@<ver>` — opt-in MinerU 4 extractor (`mineru-standard@4.2.3`);
  MinerU 3.x runs wrote `mineru-<backend>@<ver>` (`mineru-vlm@3.2.3`).
- `mammoth+markdownify@<ver>` — default DOCX extractor (no math).
- `pandoc@<ver>` — DOCX extractor used when source has OOXML math and pandoc
  is available; math is preserved as `$...$` / `$$...$$` LaTeX. Also the
  default RTF route (`extract_rtf.py`) when pandoc is on PATH.
- `striprtf@<ver>` — pure-Python RTF fallback (plain text) when pandoc absent.
- `libreoffice+<inner>@<ver>` / `textutil+<inner>@<ver>` — legacy `.doc`
  extractor (`extract_doc.py`): the converter prefix records how `.doc` was
  turned into `.docx`, `<inner>` is the DOCX extractor that ran on it
  (`mammoth+markdownify` or `pandoc`). `antiword@<ver>` — plain-text `.doc`
  fallback.
- `python-pptx@<ver>` — PPTX extractor.
- `passthrough-md@<ver>` / `passthrough-txt@<ver>` — plain text/markdown.
- `trafilatura@<ver>` — HTML extractor. Also the XHTML route inside
  `extract_xml.py` (with `markdownify@<ver>` as its fallback).
- `lxml-xml@<ver>` — generic XML extractor (text blocks, tags stripped);
  `raw-text@<ver>` — its passthrough fallback when the XML doesn't parse.
- `openpyxl@<ver>` — XLSX extractor (read-only, defusedxml).
- `ipynb@<ver>` — Jupyter notebook extractor.
- `claude-pagewise-manual@1` — reserved for manual Read-tool transcription
  used to recover content that the automated extractors lost (`mangled_visual_layout`
  warnings on PDFs, residual `dropped_pictures` after auto-recovery, etc.).

## Markdown body

PDF bodies use `[page N]` anchors between page contents:

```markdown
[page 1]

# Title

paragraph...

[page 2]

paragraph...
```

PPTX bodies use slide headings:

```markdown
## Slide 1: Title

slide body

### Notes

speaker notes

---

## Slide 2: Next title
...
```

Notebook (`.ipynb`) bodies use per-cell anchors:

```markdown
## Cell 1 (markdown)

# Notebook heading

prose…

---

## Cell 2 (code) [execution_count=1]

​```python
import numpy as np
​```

### Output

​```
array([1, 2, 3])
​```

---
```

Image content is **never** stored as base64 inline. Pictures in PDF / DOCX /
PPTX / RTF / EPUB are written to `<kb_dir>/assets/` and linked
(`![caption](../assets/<file>)`); vector images (EMF/WMF/SVG/PDF/EPS) are
rasterized to PNG first. An image that cannot be saved — too small, corrupt,
or a vector image with no rasterizer on PATH — is skipped with a warning
(`vector image (emf) not rasterized — LibreOffice is not on PATH — install …`).
With `--no-extract-images` DOCX images become `![image N: alt]()` placeholders.
Image outputs inside `.ipynb` code cells are collapsed into a single
`*(image output omitted: N)*` placeholder per cell plus a warning surfaced in
frontmatter.

PPTX objects beyond text and pictures:

```markdown
**Chart: Revenue** (column clustered)

| category | 2025 | 2026 |
| --- | --- | --- |
| Q1 | 10 | 12 |

*(SmartArt)*

- node text
- node text

> Embedded **e01** `Microsoft_Word_Document1.docx` (docx, 35.8 KB) → [doc-001_e01](../docs/doc-001_e01-microsoft-word-document1.md)
```

Charts come from the values cached in the chart XML; SmartArt is its node texts
in data-model order; an OLE object shows its preview image (unless it is shown
as an icon) and the link to its embedded document.

## Audio, video and subtitles (body)

```markdown
# lecture-03.mp4

> Machine transcript of the speech — names and terms may be misheard. [mm:ss] = start of a passage; ⟨gigaam⟩ = text from the second engine.

## 00:00–05:00

[00:00] Добрый день, коллеги. Сегодня мы разберём …

[00:41] ⟨gigaam⟩ а вот здесь whisper зациклился и текст взят у второй модели

## 05:00–10:00

[05:02] …
```

- Paragraph = consecutive segments/cues of one source, < 2.5 s apart, ≤ 500
  chars, within one section; `[h:mm:ss]` for recordings of an hour or more.
- Sections: the container's chapters when there are ≥ 2 (`## Глава 1. Введение
  (00:00–00:09)`), else 5-minute windows (none for clips under ~6 min). The
  heading path is what search citations show, so a hit reads
  `lecture-03.mp4 › 10:00–15:00`.
- Subtitle-sourced docs use the same layout; the note names the track.
  Paired sidecar files that were not the main text follow as
  `## Subtitles: <file> (<lang>)` sections.
- `extract_subtitles.py` (standalone `.srt`/`.vtt`/…) renders the same way.

## Embedded documents

Documents inside documents — an OLE object on a slide, a workbook in a Word
file, a chart's data workbook, a PDF attachment — are extracted **recursively,
by default**, each into its **own** doc:

```
docs/doc-001-lecture.md                     # parent (pptx)
docs/doc-001_e01-microsoft-word-document1.md # its embedded Word file
docs/doc-001_e01_e01-report.md              # a PDF inside that Word file
```

Why separate docs and not subsections: each child keeps its own
`source_sha256` (of the embedded bytes), `extraction_method`, `warnings`,
headings and `[page N]` anchors (which would collide with the parent's), and the
search index cites it on its own. Its `source` is the chain
`<parent source>#<member inside the parent>`, e.g.
`Лекции/a.pptx#ppt/embeddings/oleObject1.bin/report.pdf` (an OLE Package's
stored file name follows the `.bin` member), so a citation says exactly where
the text came from.

**Where it is found.** OOXML (DOCX/PPTX/XLSX, and `.doc` after its conversion to
DOCX): every `word|ppt|xl/embeddings/*` member. PDF: the `EmbeddedFiles` name
tree (incl. portfolios) and file-attachment annotations. `oleObject*.bin` OLE2
wrappers are opened with olefile: an OLE Package (`\x01Ole10Native`, with its
stored file name), a `Package` stream (OOXML), Acrobat's `CONTENTS` (PDF), or a
whole `.doc` (`WordDocument`). Payloads are classified by magic bytes, never by
the stored name, and extracted by the same extractor a corpus file of that type
gets (PDFs always through `pymupdf4llm` — never the opt-in mineru tier). Images
packaged as objects go to `assets/` and are linked in place. Equation objects
(`Equation.3`) are listed but not extracted (their preview image is what the
slide/page shows); legacy `.xls`/`.ppt`, Visio and other OLE types are listed
as not supported.

**Parent body.** PPTX puts the link where the object sits on the slide (and
after a chart for its data workbook). Everything else is listed in a closing
section:

```markdown
## Embedded objects

- **e01** `notes.docx` (docx, 35.8 KB, attachment) → [doc-002_e01](../docs/doc-002_e01-notes.md)
- **e02** `oleObject2.bin` (equation, Equation.3, 9.5 KB, document body) — not extracted: equation object — its preview image is the only rendering
- **e03** `photo.png` (image, Package, 9.5 KB)

  ![photo.png](../assets/doc-005_e03.png)
```

Links are `../docs/<file>` so they survive sharding (part files get
`../../docs/`); when a child doc is itself sharded, `shard_doc.py` repoints flat
links to its `_index.md`.

**Limits** — each cut is a warning in the parent (and propagated up to the
corpus file as `N object(s) inside it not extracted, e.g. …`), never silent:

| limit | default | knob |
|---|---|---|
| depth (corpus file = level 0) | 4 | `--embed-depth N` on `extract_corpus.py`/`dkb`, env `DOC2KB_EMBED_DEPTH`; 0 = off |
| objects examined per document | 64 | — |
| child docs per corpus file (whole tree) | 200 | — |
| one payload, uncompressed | 100 MiB | — |
| zip member compression ratio (when > 1 MiB) | 200:1 | — |
| bytes read from one container | 512 MiB | — |
| time | the parent's `--timeout` minus 15 s | env `DOC2KB_EMBED_DEADLINE` (epoch s) |

A payload whose sha256 equals an ancestor's is not re-entered (cycle); the same
payload twice in one parent is extracted once (`same content as an earlier
object`). Re-extracting a parent first deletes its old `<id>_e*` docs and
assets, so a changed set of objects never leaves stale children.

## Sharded documents (folder form)

Emitted by the opt-in `shard_doc.py` pass (Phase 4.5, enabled with
`extract_corpus.py --shard` / `dkb … --shard`). A doc whose body exceeds the
shard trigger (**24,000 tokens** by default) AND exposes usable structure is
replaced **in place**: the flat `docs/<id>-<slug>.md` becomes a folder

```
docs/<id>-<slug>/
  _index.md          # parent — frontmatter + generated TOC, no source body
  001-<part-slug>.md # part 1 (verbatim)
  002-<part-slug>.md # part 2
  ...
```

Split is **heading-first** (top Markdown heading level → deeper levels only
inside an oversized unit → `[page N]` anchors for header-less PDFs → blank-line
paragraph packing as the last resort). Token target is used **only** to pack
small units together and to sub-divide a single unit above the cap, and even
then the cut is on a block boundary — never mid-paragraph, never inside a code
fence. Parts are balanced: every part lands roughly in `[min, cap]` (defaults
**2,000 / 12,000** tokens, target **8,000**); the only part allowed past the cap
is a single indivisible paragraph (flagged `part_oversize`). Docs at or below
the trigger, or above it but lacking structure, stay flat and untouched.

**Verbatim invariant** (Rule #1): concatenating the parts in `part_order`
reproduces the original body byte-for-byte (modulo whitespace normalization and
the asset-link depth rewrite below). `shard_doc.py` self-checks this before
writing and **aborts to flat** on any mismatch — sharding can never lose content.

**Asset links** in a part are one directory deeper than the flat doc, so
`](../assets/…)` is rewritten to `](../../assets/…)` in part bodies (link
targets only; body text untouched).

Parent `_index.md` frontmatter:

| key | meaning |
|-----|---------|
| `id` / `source` / `source_type` / `source_sha256` / `extraction_method` | inherited verbatim from the original flat doc (parts share the same `id` and `source_sha256`) |
| `tokens_estimated` | sum over parts (== original body tokens) |
| `headings` / `warnings` | the original doc-level values |
| `sharded` | `true` |
| `shard_version` | shard-pass schema version |
| `shard_tool` | `doc2kb-shard@<VERSION>` |
| `part_count` | number of parts |
| `parts` | ordered list of part filenames |

Each part `NNN-<slug>.md` frontmatter:

| key | meaning |
|-----|---------|
| `id` / `source` / `source_type` / `source_sha256` | **same as parent** — citations resolve to the real source, provenance preserved |
| `parent` | `_index.md` (relative link up) |
| `part_order` | 1-based position |
| `part_title` | the structural unit's heading (or `Part N`) |
| `part_sha256` | sha256 of this part's verbatim body |
| `tokens_estimated` | this part only |
| `headings` | this part's headings |
| `warnings` | `part_oversize: …` only if an indivisible paragraph exceeds the cap |

**Consumers.** A sharded doc is ONE logical entry in `manifest.json`
(`sharded: true` + `parts[]`, `kb_path` → `_index.md`) so `total_documents`
counts the book once. The search index (`index_kb.py`) indexes the **part
bodies** — all parts share the parent `doc_id`, so per-doc filters still match,
while each chunk's `kb_path` cites the small part file an agent should open. The
parent `_index.md` is navigation, not content: it is excluded from both the
manifest document list and the search index. Idempotent: an already-sharded
folder with a matching `source_sha256` is a no-op; a source change re-shards.

## `manifest.json`

Emitted by `build_manifest.py`. Schema:

```json
{
  "schema_version": "1.0",
  "extraction_tool": "doc2kb@<VERSION>",
  "created_at": "ISO-8601 UTC",
  "corpus_root": "/abs/path/to/input",
  "total_documents": int,          // embedded docs included
  "embedded_documents": int,       // how many of them are embedded docs
  "total_tokens_estimated": int,
  "documents": [
    {
      "id": "doc-NNN",
      "source_path": "rel/path",
      "kb_path": "docs/doc-NNN-slug.md",
      "sha256": "hex",
      "source_type": "pdf",
      "extraction_method": "pymupdf4llm@0.0.x",
      "tokens_estimated": int,
      "warnings": [string],
      // ... copies relevant per-type fields from the doc's frontmatter
      // (incl. embedded_objects/embedded_docs on a parent and
      //  embedded_in/embedded_member/embedded_location/embedded_depth/
      //  parent_source_sha256 on an embedded doc)
      // Sharded docs (see "Sharded documents" below) add:
      "sharded": true,                 // present + true only for a sharded doc
      "part_count": int,               // number of part files
      "kb_path": "docs/doc-NNN-slug/_index.md",   // points at the folder TOC
      "parts": [                        // ordered, one per part file
        { "order": int, "title": "string",
          "kb_path": "docs/doc-NNN-slug/NNN-part-slug.md",
          "tokens_estimated": int }
      ]
    }
  ],
  "skipped": [
    { "source_path": "rel/path", "reason": "..." }
  ],
  "errors": [
    { "source_path": "rel/path", "error": "..." }
  ]
}
```

## `INDEX.md`

Human + agent readable. It is the single agent-facing catalog: each document
carries its `headings` and any `warnings` inline, so the agent does not need
to read `manifest.json` or `llms.txt` to navigate (those remain for tooling /
standard compliance). Generated structure:

```markdown
# Knowledge Base Index

N document(s) extracted on YYYY-MM-DD. Estimated total: ~X,XXX tokens.

## How to use
(1. AGENTS.md, 2. this catalog, 3. docs/* as needed; manifest.json/llms.txt
 are for tooling, not navigation)

## By source type
### pdf (M document(s), ~X tokens)
- [source name](docs/doc-NNN-slug.md) — Mp, ~X tok
  - Heading One · Heading Two · Heading Three
  - ⚠ warning text (only when the doc has warnings)
...

## Skipped (K)
- `path` — reason

## Errors (J)
- `path` — error
```

## `llms.txt`

llmstxt.org-compatible catalog:

```
# Knowledge Base
> N documents, ~X tokens estimated, extracted YYYY-MM-DD via doc2kb@VERSION.

## PDFs
- [source name](docs/doc-NNN-slug.md): N pages, ~X tokens
...
```

## `AGENTS.md`

Static template at `skills/doc2kb/assets/agents_template.md` is copied to
`<kb_dir>/AGENTS.md` verbatim. It instructs the second-session agent on
reading order (INDEX → manifest → docs as needed), citation policy, and
warning interpretation.

## Stdout JSON from each extract_*.py

All `extract_*.py` and `scout_corpus.py` print exactly one JSON line to
stdout. Successful extraction:

```json
{"ok": true, "out": "/abs/path/output.md", "tokens_estimated": int, "warnings": [string], ...per-type extras...}
```

Container extractors (DOCX, DOC, PPTX, XLSX, PDF via pymupdf4llm) add
`"embedded_objects": int` (found in this doc), `"embedded_docs_total": int`
(child docs written in its whole subtree) and `"embedded_not_extracted":
[string]` (objects in the subtree that were cut, `"<doc-id> <eNN> `name`:
reason"`, at most 50).

Failure:

```json
{"ok": false, "reason": "error message", ...}
```

`build_manifest.py`:

```json
{"ok": true, "kb_dir": "/abs/path", "documents": int, "tokens_estimated": int, "skipped": int, "errors": int}
```

Stderr is used only for human-readable progress logs.

## Stdout JSON from `extract_corpus.py` (Phase 4 dispatcher)

The dispatcher prints exactly one JSON object as its last stdout line. Every
`_scout.files[]` entry lands in exactly one `counts` bucket; flagged-but-
extracted files (`visual_transcription` / `dropped_pictures_residual`) are
counted in `extracted` AND listed in `needs_attention[]` with
`extracted_but_flagged: true`, so `counts.needs_attention` holds only
`needs_install` entries and `sum(counts.values()) == len(files)`.

```json
{
  "ok": true,                       // false iff error count > 0 or refused to start
  "kb_root": "/abs/kb",
  "scout": "/abs/kb/_scout.json",
  "counts": {
    "extracted": int,               // ok:true (includes extracted_but_flagged)
    "unchanged": int,               // produced doc's (flat or shard _index.md) source_sha256 == scout sha256
    "skipped_by_decision": int,     // strategy ∈ {skip, not_in_mvp, needs_ocr_or_vlm, needs_password}
    "error": int,                   // ok:false / non-2 exit / timeout / invalid scout field
    "needs_attention": int          // == number of needs_install entries only
  },
  "extracted_but_flagged": int,     // subset of `extracted` also in needs_attention[]
  "embedded_documents": int,        // child docs written for embedded objects (not in counts)
  "needs_attention": [
    { "id": "doc-007", "source_path": "rel", "reason": "needs_install",
      "doc": null, "extracted_but_flagged": false, "install_hint": "..." },
    { "id": "doc-011", "source_path": "rel", "reason": "visual_transcription",
      "doc": "docs/doc-011-...md", "extracted_but_flagged": true, "warning": "..." },
    { "id": "doc-020", "source_path": "rel", "reason": "dropped_pictures_residual",
      "doc": "docs/doc-020-...md", "extracted_but_flagged": true,
      "pages": [2, 18, 19], "warning": "..." }
  ],
  "unclassified_warnings": [ { "id": "...", "source_path": "...", "warnings": [string] } ],
  "errors_log": "/abs/kb/_logs/errors.json" | null,   // present iff error count > 0
  "elapsed_seconds": float,
  "driver": "doc2kb-extract-corpus@<VERSION>"
}
```

`reason` ∈ {`needs_install`, `visual_transcription`, `dropped_pictures_residual`}.
`pages` (list[int]) appears only for `dropped_pictures_residual`, recovered by
re-scanning the produced doc for residual `==> picture … omitted <==`
placeholders mapped to `[page N]` anchors (the warning string itself carries
only counts). `unclassified_warnings[]` echoes every unrecognized `ok:true`
warning verbatim — nothing is silently swallowed.

Refusal (missing `_scout.json`, unparseable, or any unresolved
`action_required`) prints `{"ok": false, "reason": "...", "files": [...], "driver": ...}`
and exits 2 without extracting anything.

`_logs/errors.json` (written by the dispatcher, read by `build_manifest.py`) is
the array `[{"source_path": str, "error": str}, ...]`; it is overwritten on
every run (empty `[]` on a clean run) so a stale list is never surfaced.
