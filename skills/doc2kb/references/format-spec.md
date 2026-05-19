# doc2kb — Output Format Specification

This document is the source of truth for every artefact `doc2kb` produces.
Scripts read and write to these schemas; downstream agents read them in the
second session.

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
└── docs/
    ├── <doc-id>-<slug>.md
    └── ...
```

Filenames: `<doc-id>` is `doc-NNN` (zero-padded, scout-assigned). `<slug>` is
ASCII-normalized `Path(source).stem` lowercased (cyrillic and CJK chars are
dropped). Maximum length 48 chars after the prefix.

## `_scout.json`

Emitted by `scout_corpus.py`. Schema:

```json
{
  "schema_version": "1.0",
  "scout_tool": "doc2kb@<VERSION>",
  "scanned_at": "ISO-8601 UTC",
  "input_root": "/abs/path/to/input",
  "kb_root": "/abs/path/to/kb",
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
      "source_type": "pdf"|"docx"|"pptx"|"xlsx"|"md"|"txt"|"html"|"ipynb"|"epub"|"rtf"|"odt"|"image"|"unknown",
      "pdf_class": "text"|"image_only"|"mixed"|"encrypted"|"corrupt" | null,
      "pages": int | null,
      "slides": int | null,
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
      "encoding": "utf-8" | "cp1251" | ... | null,
      "extraction_strategy": "pymupdf4llm"|"mammoth"|"python-pptx"|"passthrough-md"|"passthrough-txt"|"trafilatura"|"ipynb"|"needs_password"|"needs_ocr_or_vlm"|"not_in_mvp"|"skip",
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
| `source_type`       | string      | `pdf`/`docx`/`pptx`/`ipynb`/`md`/`txt`/`html` |
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
| `inline_images`    | docx/pptx  | count of embedded pictures |
| `has_tables`       | docx/pptx  | bool — at least one table |
| `has_equations`    | docx       | bool — OOXML `<m:oMath>` present |
| `has_charts`       | pptx       | bool — chart shapes present |
| `has_tracked_changes` | docx    | bool — `w:ins`/`w:del` present |
| `paragraphs`       | docx       | total paragraph count |
| `source_encoding`  | md/txt/html | detected source encoding |
| `cells`            | ipynb      | total cell count |
| `code_cells`       | ipynb      | code-cell count |
| `markdown_cells`   | ipynb      | markdown-cell count |
| `raw_cells`        | ipynb      | raw-cell count |
| `has_outputs`      | ipynb      | bool — any code cell has non-empty `outputs[]` |
| `language`         | ipynb      | language sniffed from `kernelspec`/`language_info`, allowlist-clamped (default `python`) |
| `kernelspec_name`  | ipynb      | original kernel name (optional, sanitized) |
| `assets`           | pdf        | list of relative paths to embedded images extracted into `<kb_dir>/assets/` and referenced from the body (only emitted when at least one image was recovered) |
| `headings`         | all        | first up to 10 top-level headings, for fast index |

`extraction_method` values that callers should recognise:

- `pymupdf4llm@<ver>` — default PDF extractor.
- `mammoth+markdownify@<ver>` — default DOCX extractor (no math).
- `pandoc@<ver>` — DOCX extractor used when source has OOXML math and pandoc
  is available; math is preserved as `$...$` / `$$...$$` LaTeX.
- `python-pptx@<ver>` — PPTX extractor.
- `passthrough-md@<ver>` / `passthrough-txt@<ver>` — plain text/markdown.
- `trafilatura@<ver>` — HTML extractor.
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

Image content is **never** stored as base64 inline. Inline images in DOCX are
replaced with `<img src="" alt="image N: original alt text">` which markdownify
renders as `![image N: ...]()`. Image outputs inside `.ipynb` code cells are
collapsed into a single `*(image output omitted: N)*` placeholder per cell
plus a warning surfaced in frontmatter.

## `manifest.json`

Emitted by `build_manifest.py`. Schema:

```json
{
  "schema_version": "1.0",
  "extraction_tool": "doc2kb@<VERSION>",
  "created_at": "ISO-8601 UTC",
  "corpus_root": "/abs/path/to/input",
  "total_documents": int,
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

Human + agent readable. Generated structure:

```markdown
# Knowledge Base Index

N document(s) extracted on YYYY-MM-DD. Estimated total: ~X,XXX tokens.

## How to use
(1. INDEX.md, 2. manifest.json, 3. AGENTS.md, 4. docs/*)

## By source type
### pdf (M document(s), ~X tokens)
- [source name](docs/doc-NNN-slug.md) — Mp, ~X tok
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

Failure:

```json
{"ok": false, "reason": "error message", ...}
```

`build_manifest.py`:

```json
{"ok": true, "kb_dir": "/abs/path", "documents": int, "tokens_estimated": int, "skipped": int, "errors": int}
```

Stderr is used only for human-readable progress logs.
