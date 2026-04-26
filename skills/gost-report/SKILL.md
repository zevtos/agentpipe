---
name: gost-report
description: Generate Russian academic reports (.docx) formatted to GOST 7.32 — лабораторные работы, отчёты по практике, курсовые проекты, ВКР, домашние задания для любого российского вуза (ИТМО, МГУ, СПбГУ, МФТИ, Бауманка, и т.д.). Use this skill whenever the user asks for a report по ГОСТ, лабораторную работу, отчёт по практике, курсовой проект, ВКР, или любой Russian-language student paper that needs proper title page, headings, page numbers, figure/table captions. Trigger this skill even if the user only mentions "лабораторная" or "отчёт" without naming a specific university — Russian-language context (references to ИТМО / МГУ / СПбГУ / университет / ГОСТ) is enough. ITMO is the default profile (preserves the original itmo-report behavior); other universities are supported via UniversityProfile.
---

# GOST report generator

Producing a fully GOST-compliant Russian academic report by hand with python-docx is fiddly: dozens of formatting rules (margins, fonts, line spacing, page numbers starting from page 2, heading sizes, figure/table captions, list numbering reset, and so on). This skill wraps all of that into a small high-level API so you only write content, not formatting.

## When to use

Use this skill whenever the user asks you to generate any of:
- Лабораторная работа (lab report)
- Отчёт по практике (internship/practice report)
- Курсовой проект (term project)
- ВКР / выпускная квалификационная работа (thesis)
- Домашнее задание / отчёт по заданию
- Any other Russian-language academic paper that needs ГОСТ formatting

Do **not** use it for non-Russian/non-GOST documents, presentations, or anything that isn't a written academic paper.

## How to use

The library file is `scripts/gost_report.py`. Copy it next to your generation script (or import from this skill's `scripts/` directory), then write a small script that calls the API.

### Profiles — picking your university

Different universities tweak GOST 7.32 in their own way (margins, heading sizes, «ОГЛАВЛЕНИЕ» vs «СОДЕРЖАНИЕ», etc.). The skill ships two profiles and lets you build your own:

| Profile | Use when |
|---|---|
| `ITMO_PROFILE` (default) | ITMO University, ФПИиКТ. Margins 30/20/10/10 (title) and 30/10/20/20 (body), heading sizes 16/15/14, all centered, «ОГЛАВЛЕНИЕ». |
| `GOST_PROFILE` | Any other vuz that follows pure GOST 7.32-2017. Margins 30/15/20/20, heading sizes 16/14/14, h2/h3 left-aligned, «СОДЕРЖАНИЕ». University name fields are blank — fill them via `TitleConfig`. |
| Custom `UniversityProfile(...)` | Anything else — override exactly the fields that differ from `GOST_PROFILE`. |

### Minimal workflow (ITMO — default)

```python
from gost_report import Report, TitleConfig

r = Report(TitleConfig(
    work_type="Лабораторная работа",
    work_number="№1",
    topic="Основы работы в командной строке Unix",
    student_name="Фамилия И.О.",
    student_group="P3XXX",
    teacher_name="Фамилия И.О.",
    teacher_degree="к.т.н.",
    teacher_position="доцент",
    teacher_label="Проверил",                  # or "Руководитель" for ВКР
    year="2026",
))

r.toc()
r.h1("Введение")
r.text("Цель работы — ...")
r.h1("Выполнение работы")
r.task("Задание 1. ...")
r.code("ls -la")
r.text("Результат показывает ...")
r.h1("Заключение")
r.numbered(["Выполнено задание 1.", "Выполнено задание 2."])

r.save("/mnt/user-data/outputs/report.docx")
```

### Other universities

Pass a `UniversityProfile` (built fresh, or by tweaking `GOST_PROFILE`):

```python
from gost_report import Report, TitleConfig, UniversityProfile

SPBSU_PROFILE = UniversityProfile(
    university_short="«Санкт-Петербургский государственный университет»",
    faculty="Математико-механический факультет",
    city="Санкт-Петербург",
    # GOST 7.32 defaults for everything else (margins, heading sizes, «СОДЕРЖАНИЕ»)
)

r = Report(
    TitleConfig(
        work_type="Курсовая работа",
        topic="Численные методы решения СЛАУ",
        student_name="Фамилия И.О.",
        student_group="20.Б10-мм",
        year="2026",
    ),
    profile=SPBSU_PROFILE,
)
```

If you build a profile for a vuz that isn't in the library yet, please send it back as a PR — over time this skill should ship presets for every major Russian university.

### API reference

`UniversityProfile` — dataclass: title/body margins (mm), heading sizes (pt) and alignment ("center"/"left"), `h1_uppercase`, `h1_new_page`, `toc_title`, plus `ministry`, `university_full`, `university_short`, `faculty`, `city`. All fields have GOST-7.32 defaults.

`TitleConfig` — content of the title page. Required: `work_type`, `topic`, `student_name`, `student_group`, `year`. Optional: `work_number`, `variant`, `teacher_name`, `teacher_label` (default `"Проверил"` — change to `"Руководитель"` for ВКР), `teacher_degree`, `teacher_position`. University fields (`city`, `ministry`, `university_full`, `university_short`, `faculty`) are blank by default and inherit from the profile; set them on `TitleConfig` only to override per-document.

`Report(title_config, profile=ITMO_PROFILE)` — creates the document and builds the title page. Pass `profile=GOST_PROFILE` or your own `UniversityProfile(...)` for non-ITMO usage.

Content methods (call in order):

| Method | What it does |
|---|---|
| `r.toc()` | Inserts an automatic table of contents field titled per `profile.toc_title`. The TOC is empty until the user opens the file in Word and right-clicks → "Update field". Tell the user this. |
| `r.h1(text)` | Heading 1 (chapter / structural element). Bold, sized per profile. Auto-uppercased and starts on a new page if the profile says so. |
| `r.h2(text)` | Heading 2. Bold, sized per profile, alignment per profile. |
| `r.h3(text)` | Heading 3. Bold, sized per profile, alignment per profile. |
| `r.text(text, bold=False, italic=False)` | Body paragraph: justified, first-line indent 1.25 cm, TNR 14, line spacing 1.5. |
| `r.task(text)` | A task statement, bold + justified. Use for «Задание 1. ...» blocks. |
| `r.code(code_str)` | Multi-line monospaced code/command block (Courier New 11), no first-line indent. |
| `r.figure(image_path, caption, width_cm=None)` | Image + auto-numbered caption «Рисунок N — Описание» below, both centered. |
| `r.table(rows, caption, has_header=True)` | Table with auto-numbered caption «Таблица N — Описание» above. `rows` is a list of lists of strings; first row is bold header by default. |
| `r.numbered(items)` | Numbered list. Each call starts numbering at 1 again — independent of prior lists. |
| `r.bullet(items)` | Bulleted list. Same independence as numbered. |
| `r.page_break()` | Forced page break (rarely needed — h1 already breaks for you when `h1_new_page=True`). |
| `r.save(path)` | Saves the .docx to the given path. |

### Behaviour notes worth remembering

- **No quotes around the topic.** The library passes `topic` as-is — don't wrap it in «» or "" yourself; GOST forbids quotes around the topic.
- **First h1 doesn't double-break.** After `r.toc()` and after the title page, the first h1 won't insert a redundant page break.
- **Lists restart at 1.** Each `r.numbered(...)` call begins a fresh numbered list. To continue a previous one, pass all items in a single call.
- **The TOC is a Word field.** It renders as a placeholder in PDF preview / LibreOffice unless updated. Tell the user: "Откройте файл в Word, ПКМ по оглавлению → 'Обновить поле'".
- **Don't manually tweak fonts/margins.** Build a `UniversityProfile` instead. If the user asks for unusual formatting (e.g. landscape page), drop down to raw python-docx via `r.doc`.
- **Use placeholder names in examples** ("Фамилия И.О."), never real ones — the user fills in their actual name.

### Typical content patterns

**Лабораторная работа** — title page → toc → h1 «Введение» → h1 «Выполнение работы» (with h2 per task, then `task()` + `code()` + `text()` + optional `figure()`) → h1 «Заключение» (numbered list of results).

**ВКР / курсовой проект** — set `teacher_label="Руководитель"`. Add актуальность, объект, предмет, цель, задачи to «Введение». Use h1 «Глава 1. ...», h1 «Глава 2. ...», h1 «Заключение», h1 «Список литературы».

**Отчёт по практике** — `work_type="Отчёт по практике"`, structure usually: введение → описание организации → выполненные работы → заключение.

## Dependencies

- `python-docx` (pip install python-docx)
- Optional: `Pillow` if the user provides images that need preprocessing.

## Checklist after generating

Before delivering the file, verify:
- [ ] Title page renders correctly (open the .docx or convert to PDF for preview)
- [ ] Page numbering starts at page 2 (title page has no number)
- [ ] All `figure()` calls have captions; all `table()` calls have captions
- [ ] Headings are sized correctly and on new pages where required
- [ ] No real names slipped into examples — use «Фамилия И.О.» as placeholder

If you have access to a code execution environment, run a quick PDF preview to sanity-check the layout. For a more rigorous check, see `references/checklist.md`.
