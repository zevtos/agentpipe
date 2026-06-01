# doc2kb — Batch User Questions Template

When `scout_corpus.py` produces a `_scout.json` with non-empty
`user_decisions_needed`, you MUST batch all questions into **one message**
to the user. Below are templates for each decision group.

## Master template

```
Я просканировал корпус: <N> файл(ов), ~<TOK> токенов, ~<MB> MB.

Нашёл <K> групп(ы) файлов, требующих решения:

1. <type>: <count> файл(ов)
   <list of files, one per line>
   Варианты: <option1> / <option2> / ...
   По умолчанию: <default>

2. ...

Ответьте одним сообщением, например:
  '1: skip; 2: <вариант>'
```

Use Russian or the user's language. Use exactly the option names listed in
`user_decisions_needed[].options`.

## Per-decision-type wording

### `encrypted`

```
Зашифрованные файлы (нужен пароль или skip):
  - contracts/nda.pdf
  - contracts/agreement.pdf
Варианты: password / skip
По умолчанию: skip
```

If user provides a password: re-run scout with the password (future
enhancement — MVP does not auto-retry; just skip).

### `scanned_pdf`

```
Сканированные PDF (нет text-слоя):
  - scans/old_textbook.pdf (412 страниц)
Варианты: skip / ocr_tesseract / vlm_mlx / claude_pagewise
По умолчанию: skip

В текущем MVP реально поддерживается только 'skip'. OCR/VLM придут в
follow-up commits — пока укажите 'skip', чтобы продолжить.
```

### `huge_file`

```
Большие файлы (>500 страниц или >50 MB):
  - books/textbook.pdf (964 страницы, 47 MB)
Варианты: skip / proceed / split
По умолчанию: skip

proceed = извлечь целиком (может занять минуты, добавит ~600k токенов в kb).
split = пока не поддержано, эквивалентно proceed.
```

### `corrupt`

```
Файлы, которые scout не смог открыть:
  - data/broken.pdf
Варианты: skip
По умолчанию: skip
```

### `unsupported_format`

```
Форматы, не поддержанные в MVP:
  - .xlsx, .epub, .odt, изображения
Варианты: skip
По умолчанию: skip

Поддержка этих форматов придёт в follow-up release.
```

(`.doc` и `.rtf` поддержаны и сюда не попадают. `.doc` требует системного
конвертера — `soffice`/`libreoffice`, macOS `textutil`, или `antiword`; если
его нет, scout добавит warning к файлу, а `extract_doc.py` вернёт install-hint.
`.rtf` всегда извлекается — pandoc при наличии, иначе pure-Python `striprtf`.)

## Example end-to-end question

If scout produces:

```json
"user_decisions_needed": [
  {"type": "encrypted", "files": ["contracts/a.pdf"], "options": ["password","skip"], "default": "skip"},
  {"type": "scanned_pdf", "files": ["scans/book.pdf"], "options": ["skip","ocr_tesseract","vlm_mlx","claude_pagewise"], "default": "skip"},
  {"type": "huge_file", "files": ["books/big.pdf"], "options": ["skip","proceed","split"], "default": "skip"}
]
```

The single message to the user should look like:

```
Я просканировал корпус: 47 файлов, ~1.24M токенов, ~184 MB.

Нашёл 3 группы файлов, требующих решения:

1. Зашифрованные PDF (1):
   - contracts/a.pdf
   Варианты: password / skip
   По умолчанию: skip

2. Сканированные PDF (1, MVP поддерживает только skip):
   - scans/book.pdf
   Варианты: skip / ocr_tesseract / vlm_mlx / claude_pagewise
   По умолчанию: skip

3. Большие файлы (1, >50 MB):
   - books/big.pdf (964 страницы, 47 MB)
   Варианты: skip / proceed / split
   По умолчанию: skip

Ответьте одним сообщением, например:
  '1: skip; 2: skip; 3: proceed'

Или просто 'все по умолчанию' — тогда все три группы будут пропущены.
```

## After the user replies

Apply their answers to update `_scout.files[].extraction_strategy`:
- `password` → file gets a new `extraction_strategy: "needs_password_retry"`
  (MVP: still skip — feature lands in follow-up).
- `skip` for any group → set `extraction_strategy: "skip"`.
- `proceed` for huge_file → keep its existing strategy (`pymupdf4llm` etc.).
- `ocr_tesseract` / `vlm_mlx` / `claude_pagewise` → MVP: treat as skip,
  warn the user that these require follow-up release.

Then proceed to Phase 4 (Extract).
