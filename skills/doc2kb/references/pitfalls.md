# doc2kb — Pitfalls (what NEVER to do)

## 1. Никогда не суммаризируй

Пользователь хочет **verbatim equivalent** того, что человек прочитал бы.
Суммаризация теряет факты, и потерянный факт не возвращается без
re-extraction (а исходный файл к тому моменту может быть недоступен).

Разрешено:
- структурная очистка (`normalize_md.py`) — дедупликация header/footer на
  >70% страниц, whitespace, ASCII control characters, известный boilerplate
  (Page X of Y, ©, "Click here…", "JavaScript required").

Запрещено:
- переписывание формулировок ("в ходе работы было…" → "сделано…"),
- абстрактные пересказы,
- удаление "избыточного" контента,
- перевод,
- объединение нескольких страниц в "summary".

## 2. Никогда не пропускай scout

Соблазн "у меня всего 3 PDF, просто прогоню extract" — это путь к тихим
ошибкам. Scout даёт оценку токенов, ловит encrypted и scanned файлы, и
батчит вопросы к пользователю в одно сообщение. Без scout вы:
- упустите encrypted PDF (extract_pdf_pymupdf4llm на нём упадёт),
- сохраните пустой Markdown из image-only PDF (см. пункт 3),
- не сможете оценить общий объём корпуса до начала работы.

## 3. Никогда не сохраняй пустой результат как успешный

Naive `pypdf.extract_text()` на сканированном PDF возвращает пустую строку
без exception. Каждый extract-скрипт ОБЯЗАН проверять минимальную длину
результата:
- `extract_pdf_pymupdf4llm.py` — `< pages * 30 chars` → warning.
- `extract_docx.py` — `< 50 chars` → warning.

Warnings попадают во frontmatter и в `manifest.json` — `build_manifest.py`
видит их и отражает в INDEX.

## 4. Никогда не embed-ь картинки как base64

DOCX часто содержит inline-images. По умолчанию `mammoth.convert_to_html`
эмбедит их как `<img src="data:image/png;base64,...">`. Это:
- катастрофически раздувает токены (одна диаграмма = десятки KB body),
- бесполезно для LLM — модель не видит пиксели в base64,
- ломает downstream-tooling, которое ожидает чистый Markdown.

`extract_docx.py` уже заменяет image_handler на placeholder
`<img src="" alt="image N: original alt text">`. Не переопределяйте это
поведение.

## 5. Никогда не игнорируй speaker notes в PPTX

Speaker notes в PowerPoint часто содержат больше семантики, чем слайды
(речь автора). Большинство alternative-конвертеров (`markitdown`,
`unstructured`) их теряют. `extract_pptx.py` всегда читает
`slide.notes_slide.notes_text_frame.text` и помещает их в раздел
`### Notes` под каждым слайдом.

## 6. Никогда не задавай пользователю серию вопросов

Если scout нашёл 3 encrypted PDF + 1 scanned PDF + 2 huge файла — это
ОДИН вопрос пользователю, не 6. Используй шаблон из
`references/batch-questions.md`. Иначе агент будет 6 раз останавливаться
и пользовательский опыт будет ужасным.

## 7. Никогда не используй `markitdown` или `unstructured` как замену

- `markitdown` теряет speaker notes в PPTX, теряет структуру таблиц в
  сложных DOCX, не делает encryption detection.
- `unstructured` в API/cloud-режиме отправляет данные наружу — это
  нарушает local-first принцип; localmode тяжёлый и менее точный, чем
  специализированные per-format экстракторы.

Per-format-best-tool stack (pymupdf4llm + mammoth + python-pptx + trafilatura)
даёт лучшее качество при минимальном bundle size.

## 8. Никогда не запускай heavy-tier extract без явного opt-in

(Это для follow-up commits, не для MVP.) `docling` тянет ~1 GB моделей с
HuggingFace; `marker-pdf` плюс модели — ещё столько же; `mlx-vlm` нужен
16+ GB RAM. Эти зависимости устанавливаются ТОЛЬКО при `DOC2KB_HEAVY=1`
или `DOC2KB_VLM=1` (см. `bootstrap.sh` в follow-up release).

## 9. Никогда не нормализуй имена/термины в исходном тексте

Сохраняй оригинальное написание. LLM лучше работает с реальными данными,
даже если автор написал "ВКР" в одном месте и "вкр" в другом, или
использует "—" вместо "-". Это семантически разные знаки и часто несут
информацию о стиле/контексте.

## 10. Никогда не сливай все файлы в один большой `corpus.md`

Per-source файлы + manifest — это и есть progressive disclosure. Один
большой файл проваливается в context rot на корпусах >100K токенов.
Структура `kb/docs/<id>-<slug>.md` уже даёт агенту во второй сессии
точечный доступ через `Grep`/`Read`.

## 11. Не доверяй расширению файла

`scout_corpus.py` всегда сверяет `python-magic` MIME с расширением. При
расхождении выставляет `mime_confidence: "low"` и добавляет warning. Это
важно для security (polyglot-файлы) и для корректности (когда пользователь
переименовал .docx в .pdf).

## 12. Не пиши binary content в kb/docs/*.md

Output файлы должны быть чистым UTF-8 Markdown. Никакого base64, никаких
binary blobs, никаких embedded fonts. Если extract-скрипт натолкнулся на
binary blob (chart, OLE object) — он добавляет placeholder типа `*(chart)*`
и warning в frontmatter.

## 13. Не доверяй pymupdf4llm на PDF с визуальным layout-ом математики

Старые PDF (часто .doc → "Сохранить как PDF" в Word), научные методички,
курсовые с формулами часто содержат уравнения, набранные позиционно:
символы — отдельные `Tj`-операторы, дробные черты — `re`-прямоугольники,
штрихи производных — самостоятельные glyph'ы рядом с буквой. pymupdf4llm
читает text-layer в "logical order" и ведёт себя на таких документах
двумя характерными способами:

1. **Mangled cells.** Часть формул попадает в markdown-таблицы — там
   pymupdf4llm рассыпает их в цепочки `|x|<br>|2|<br>|y|<br>|...` —
   выглядит как мусор. Сигнатура — много `<br>` с одиночными
   фрагментами между ними.
2. **Dropped pictures.** Часть формул pymupdf4llm не может разобрать
   совсем — он эмитит `==> picture [WxH] intentionally omitted <==`
   плейсхолдер. На лабах и научных PDF эти "picture" чаще всего и есть
   уравнения, матрицы, блок-схемы. Текст вокруг ссылается на "уравнение
   (1)", "формулу", "матрицу A" — а самих формул в kb нет.

Иногда оба варианта одновременно в одном PDF.

Это **не баг pymupdf4llm**, а ограничение text-layer extraction: математика
в таких PDF просто не представлена как читаемый текст, она нарисована.

Что делает `extract_pdf_pymupdf4llm.py`:
- `_detect_mangled_layout()` считает долю мангленных ячеек в markdown-
  таблицах (`<br>`-цепочки одиночных фрагментов); если ≥ 25% data-cells
  такие → emit warning `mangled_visual_layout: ...`;
- `_detect_dropped_pictures()` считает количество `==> picture [WxH]
  intentionally omitted <==` плейсхолдеров; если ≥ 2/page или ≥ 5 на
  документ → emit warning `dropped_pictures: ...`.

Что должен делать agent при любом из этих warning'ов:
1. Прочитать PDF напрямую через `Read` (Claude рендерит страницы);
2. Переписать body соответствующего `kb/docs/<id>-*.md` вручную с
   корректными формулами (Unicode `ÿ`, `ẏ`, sub/superscripts, либо
   LaTeX-нотация);
3. Обновить frontmatter:
   - `extraction_method: claude-pagewise-manual@1`,
   - warning заменить на пояснение, что транскрипция ручная.
4. Перезапустить `build_manifest.py`.

Чего делать **нельзя**:
- "почистить" garbled output регулярками или попытаться восстановить
  дропнутые картинки текстом из соседних абзацев — потеряются формулы;
- молча принять output `pymupdf4llm` — внутри knowledge base окажется
  семантический мусор, который дойдёт до второй сессии и поломает все
  ответы про формулы;
- автоматически попробовать `pymupdf.get_text()` без markdown layout —
  тот же бэкенд, та же проблема.

## 14. Нормализуй имена файлов в Unicode NFC

macOS HFS+/APFS возвращает Cyrillic/accented filenames в NFD (decomposed)
форме: `й` идёт как `и + ́` (U+0438 + U+0306). Большинство YAML-парсеров
и Python-кода работают с NFC. Если `_scout.json` хранит путь в NFD, а
extract пишет frontmatter `source:` в NFC, путь "выглядит одинаково", но
сравнивается как разный — `build_manifest.py` помечает файл как
"extraction missing" даже когда `docs/<id>-*.md` есть на диске.

Фикс: `scout_corpus.py:scan_file()` и `_common.validate_source_rel()`
нормализуют `source_path` в NFC при создании. `build_manifest.merge_scout()`
делает дополнительный belt-and-suspenders — сравнивает обе стороны
NFC-нормализованно, чтобы старые `_scout.json` не ломали повторную сборку.
