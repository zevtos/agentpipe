# Стиль вуза: профиль и стилевые файлы

Разные вузы по-своему трактуют ГОСТ 7.32 (поля, кегли, «ОГЛАВЛЕНИЕ» или «СОДЕРЖАНИЕ», тире в подписях, переносы, списки «1)» или «1.», таблицы кеглем 12, список источников по ГОСТ 7.1 или Р 7.0.100), а школьные требования часто вообще не ГОСТ. Всё это — **стиль** (`UniversityProfile`): build.py пишет только содержание, оформление приходит из стиля. Как класс документа в LaTeX: один файл стиля на вуз, переиспользуется во всех работах.

**Методичка с требованиями (ppt/pptx/pdf/docx) → стиль:** извлеки правила (doc2kb; у `.ppt` сначала `soffice --headless --convert-to pptx`), посмотри скриншоты настроек Word на слайдах, затем сопоставь каждое требование полю (`python3 scripts/styles.py fields`). Начни с `references/templates/style.toml` или с `extends` ближайшего встроенного стиля. Чего нет в полях — не правь `r.doc` руками и не наследуйся от `Report`, а добавь поле в `UniversityProfile` (дефолт = прежнее поведение, чтобы ИТМО/ГОСТ не поменялись байт-в-байт) и описание в `styles.FIELD_DOCS`.

## Встроенные стили

| Стиль | Когда |
|---|---|
| `itmo` (по умолчанию, `ITMO_PROFILE`) | ИТМО, ФПИиКТ. Поля титульника 30/20/10/10 мм, основного текста 30/10/20/20. Кегли заголовков 16/15/14, все по центру. «ОГЛАВЛЕНИЕ». |
| `gost` (`GOST_PROFILE`) | Чистый ГОСТ 7.32-2017. Поля 30/15/20/20 мм. Кегли 16/14/14, h2/h3 по левому краю. «СОДЕРЖАНИЕ». Поля вуза пустые — заполни через `TitleConfig` или свой стиль. |
| `mbi` (`MBI_PROFILE`, `styles/mbi.toml`) | Положение о ВКР МБИ им. А. Собчака (2022). Короткое тире «–» в подписях и формальных конструкциях, автоперенос, неразрывные пробелы по п. 10.1.6, 10.8.7, 10.11.2, h2/h3 с абзаца по ширине с пропуском строки, таблицы кеглем 12 с повтором шапки и «Продолжение таблицы N», списки «–», «1)», «а)» с абзацного отступа, формулы 12 пт сверху и снизу, «где» с отступа, примечания «Примечание – …», список источников по ГОСТ Р 7.0.100–2018 (не меньше 25), приложения кеглем 12, титул по прил. 11, все дополнительные проверки. |

```python
from gost_report import Report, TitleConfig
r = Report(TitleConfig(...), profile="mbi")   # или profile=MBI_PROFILE
```

## Стилевой файл

TOML (или JSON с той же структурой). Ключ верхнего уровня — поле `UniversityProfile`, секция — группа полей с короткими именами:

```toml
# <project>/.gost-report/styles/vkr.toml
description = "ВКР Иванова: МБИ, нумерация по главам"
extends = "mbi"               # встроенный стиль, имя другого файла или путь; можно список

numbering = "chapter"         # «Рисунок 1.1», «Таблица 2.3», «(1.2)»

[headings]
numbering = true              # «1 НАЗВАНИЕ», «1.1 Подраздел» (структурные элементы без номера)

[h2]
space_before = "1line"        # число = пт; строка с единицей: "1line", "12pt", "0.5cm"

[table]
font_size = 12
continuation = true
```

`extends` без значения — дефолты `UniversityProfile` (= `gost`). Несколько родителей: следующий перекрывает отличия от дефолтов предыдущего. Имя файла становится `profile.name`, его каталог — `profile.base_dir` (от него ищется `title_docx`).

**Секции:** `[page]` (поля и номер страницы основного текста: `margin_left`, `number_position`, …), `[body]` (`font`, `font_size`, `line_spacing`, `indent_cm`, `block_space_pt`, `dash`, `prose_dashes`, `nbsp`, `hyphenation`, `widow_control`, `language`, `markup`), `[headings]`, `[h1]` `[h2]` `[h3]` (`size`, `align`, `indent_cm`, `space_before`, `space_after`, `bold`, `italic`; у h1 ещё `uppercase`, `new_page`), `[caption]`, `[table]`, `[list]`, `[formula]`, `[note]`, `[keywords]`, `[subtitle]`, `[code]`, `[bib]`, `[appendix]`, `[toc]` (`title`), `[title]` (поля титула и `[[title.blocks]]`). Внутри секции можно писать и полное имя поля. Неизвестный ключ, неверный тип, цикл `extends` — ошибка с подсказкой ближайших имён.

**Где ищется стиль по имени** (первый найденный): каталог build.py и его `.gost-report/styles/`, `.gost-report/styles/` вверх по дереву проекта, `~/.config/gost-report/styles/` (свои стили на все проекты), встроенные `<skill>/styles/`, Python-пресеты `gost` и `itmo`. Файл проекта с именем встроенного стиля перекрывает встроенный.

**Как выбрать стиль:** `Report(profile="mbi")`, `Report(profile="путь/к/стилю.toml")`, `Report(profile=load_profile("mbi", numbering="chapter"))` (поля поверх стиля), объект `UniversityProfile(...)`. Без `profile=` — `GOST_REPORT_PROFILE` из env или `<курс>/.gost-report.env`, иначе ИТМО.

**CLI** (`python3 <skill>/scripts/styles.py`, без python-docx сам перезапускается в venv скилла):

| Команда | Что делает |
|---|---|
| `fields [секция]` | Все поля: короткое имя, полное имя, дефолт, описание, тип |
| `show <стиль> [--all]` | Итоговые значения стиля в TOML (по умолчанию только отличия от ГОСТ) |
| `check <файл>` | Загрузить и проверить файл стиля |
| `list` | Какие стили доступны и откуда |

## Поля по группам

Полный список с дефолтами — `styles.py fields`. Здесь — что за чем стоит. Все дефолты повторяют прежнее поведение ИТМО/ГОСТ.

**Страница и текст.** Поля `body_margin_*` / `title_margin_*` (мм), `body_font_size`, `line_spacing`, `first_line_indent_cm`, `font_name` (гарнитура всего документа), `block_space_pt` (интервал вокруг рисунков, подписей таблиц, заданий; 0 — без межабзацных интервалов), `page_number_position` (`<top|bottom>-<left|center|right>`), `page_number_size`. Номер страницы на титуле всегда скрыт, со следующей страницы виден.

**Тире, пробелы, переносы.**
- `dash` — знак в «Рисунок 1 — …», «Примечание — …», записях Р 7.0.100.
- `prose_dashes`: `strip` (тире из прозы убираются — правило №1 скилла), `normalize` (« - », « — » между словами → « <dash> », «45—60» → «45–60», дефис в словах и «ГОСТ 7.32-2017» не трогаются), `keep` (как `allow_dashes=True`).
- `nbsp`: `off`, `required` (инициалы не отрываются от фамилии и друг от друга, число от единицы и слова-счётчика «14 пт», «2026 г.», «13–16 с.», «XX в.», «№ 5», «п. 2.6», «рисунке 1.1», «т. е.», без склейки предлогов), `full` (вся типографика режима `typography`).
- `auto_hyphenation` — автоперенос и язык ru-RU без остальной типографики; заголовки без переносов. `caption_hyphenation=false` — подписи тоже без переносов. Ключевые слова не переносятся всегда.
- `heading_glue`, `caption_glue` — предлоги и союзы не остаются в конце строки заголовка или подписи.
- `widow_control` — запрет висячих строк; `language` — язык документа; `markup` — `**жирный**` и `*курсив*` в прозе.

**Заголовки.** `heading_size_h1..h3`, `heading_align_h1..h3` (`center` | `left` | `right` | `justify`), `heading_indent_cm_h1..h3`, `heading_space_before_h1..h3` / `heading_space_after_h1..h3` (пт или `"1line"`; два заголовка подряд не складывают интервалы), `heading_bold_*`, `heading_italic_*`, `heading_line_spacing`, `h1_uppercase`, `h1_new_page`, `toc_title`. Автонумерация: `heading_numbering = true` даёт «1», «1.1», «1.1.1» без точки (`heading_number_sep` между номером и текстом); заголовок, уже начинающийся с номера, и элементы из `unnumbered_headings` («ВВЕДЕНИЕ», «ЗАКЛЮЧЕНИЕ», «РЕФЕРАТ», «СПИСОК ИСПОЛЬЗОВАННЫХ ИСТОЧНИКОВ», «ВЫВОДЫ ПО ГЛАВЕ*», …; `*` — по началу) номер не получают. `r.h1(..., numbered=False)` — вручную; `toc=False` — вне оглавления.

**Подписи и нумерация.** `figure_label`, `table_label`, `figure_caption_align`, `table_caption_align`, `table_caption_indent_cm`, `caption_font_size`, `caption_line_spacing`, `caption_keep_with_next` (подпись таблицы не отрывается от таблицы, рисунок от подписи). `numbering = "chapter"` — номера по главам «1.2» (глава — номер h1: автономер или число в начале текста «2 Проектирование» / «Глава 2.»), `formula_numbering` — отдельно для формул. В приложении номер всегда «А.1».

**Таблицы.** `table_font_size`, `table_line_spacing`, `table_header_bold`, `table_header_align`, `table_repeat_header` (шапка на каждой странице), `table_cant_split` (строка не рвётся), `table_keep_together` (таблица целиком на странице), `table_continuation` + `table_continuation_text` («Продолжение таблицы {n}»). Продолжение расставляет `r.save()` по вёрстке LibreOffice (см. `references/api.md`, «Проход вёрстки»).

**Перечисления.** `list_layout`: `hanging` (как раньше, висячий отступ) или `paragraph` (маркер на абзацном отступе, строки от левого поля, после маркера пробел, по ширине). `bullet_char` (`"–"`), `numbered_format` (`"%1)"`), `lettered_format` (`"{letter})"`), `list_letters` (алфавит `r.lettered`, без ё з й о ч ъ ы ь), `list_nested_indent_cm`.

**Формулы, примечания, реферат.** `formula_space_pt` (интервал до и после), `formula_where_indent` («где» с абзацного отступа); `note_label`, `notes_label`, `example_label`, `examples_label`, `note_font_size`; `keywords_min` / `keywords_max` / `keywords_align`, `volume_template` (строка объёма `r.volume_line`: `{pages}`, `{figures}`, `{tables}`, `{sources}`, `{sources_word}`, `{appendices}`, `{formulas}`); `subtitle_italic` / `subtitle_bold` / `subtitle_line_spacing` / `subtitle_space_after` / `subtitle_attach` (интервал после заголовка уходит под подзаголовок — шапка «НАБРОСКИ …» + строка под ней + пустая строка).

**Код, источники, приложения.** `code_font_name`, `code_font_size`, `code_indent_cm`; `bib_style` (`7.1-2003` | `7.0.100-2018`), `bib_title`, `bib_align`, `bib_indent_cm`, `bib_min_sources`; `appendix_label`, `appendix_letters`, `appendix_font_size`.

**Проверки.** `checks` — дополнительные WARN валидатора: `double-space`, `heading-period`, `paragraph-spacing`, `table-mention` (нет «таблица N» в тексте до таблицы), `figure-mention`, `keywords-count`, `bib-count`; `["all"]` — все. Правила стиля (тире, подписи, нумерация) пишутся в sentinel рядом с .docx: Stop-хук и `validate.py --check` проверяют тем же стилем.

## Титульный лист

Четыре способа, выбирается первый заданный: `title_builder` (код) → `title_docx` (бланк) → `title_blocks` (блоки) → встроенная раскладка.

**Встроенная раскладка** (ИТМО и ГОСТ): министерство, вуз, факультет, вид работы, тема, правый блок студента и преподавателя, город и год внизу. Поля: `title_size_large`, `title_right_block_indent_cm`, `title_student_layout` (`group_line` — «Группа: …», ниже «Выполнил: ФИО»; `label_group` — «<u>Выполнила</u>: ученица 9 «Б» класса», ФИО строками ниже), `title_group_template` (`"Группа: {group}"`).

**Блоки** `[[title.blocks]]` — титул по форме вуза без кода, сверху вниз:

```toml
[title]
required = ["topic", "student_name", "year"]       # обязательные поля этого титула
block_defaults = { line_spacing = 1.0 }            # общее для всех блоков

[[title.blocks]]
lines = ["{university_full}", "{university_short}"]

[[title.blocks]]
lines = ["«Допущена к защите»", "Протокол № {protocol|____} от {protocol_date|________} г."]
align = "right"
blank_before = 3

[[title.blocks]]
text = "{topic}"
bold = true
size = 16
blank_before = 3

[[title.blocks]]
runs = [{ text = "Выполнил", underline = true }, { text = ": {student_name}" }]
align = "left"
indent_cm = 9

[[title.blocks]]
text = "{city}, {year}"
anchor = "bottom"            # прижать к нижнему полю
```

Ключи блока: `text` (строка, `\n` — новая строка) | `lines` (список строк) | `runs` (одна строка из кусков со своим начертанием); `align`, `size`, `bold`, `italic`, `underline`, `caps`, `indent_cm`, `line_spacing`, `space_before`, `space_after`, `blank_before`, `blank_after` (пустые строки), `anchor = "bottom"`, `if` / `unless` (поле или список полей: блок только если заполнены / пусты), `keep_empty`. `{поле}` — значение из `TitleConfig`, `TitleConfig.extra`, профиля; `{поле|заглушка}` — заглушка для пустого («____» под подпись от руки). Строка, ставшая пустой, пропускается. Вычисляемые поля: `{student_label}` («Выполнил» / «Выполнили»), `{students}`, `{work_title}`, `{teacher_line}`, `{teacher_regalia}` («канд. экон. наук, доцент»). Поля `TitleConfig` для ВКР: `department`, `direction`, `program`, `course`, `study_form`, `head_label`, `head_name`, `head_degree`; остальное — `extra={"protocol": "5", ...}` или env `GOST_REPORT_EXTRA_PROTOCOL`.

**Бланк .docx** — когда вуз выдаёт титул файлом: `title_docx = "titul.docx"` (путь от файла стиля, корня проекта или `.gost-report/`). Первая секция бланка переносится как есть, с логотипом и ссылками; `{поля}` в тексте подставляются так же, как в блоках (Word режет текст на куски — поле находится по склеенному тексту абзаца); поля страницы титула берутся из бланка. Разрывы страниц и нумерация списков бланка убираются.

**Код** — `title_builder = callable(report)` (только в Python-профиле): внутри `report.title_config`, `report.title_value(name)`, `report.add_text_paragraph(...)`, `report.add_runs_paragraph([StyledRun(...)])`, `report.add_blank_paragraphs(n)`, `report.add_bottom_block([строки])`.

## МБИ: пример целиком

```python
from gost_report import Report, TitleConfig

r = Report(TitleConfig(
    topic="Автоматизация учёта обращений граждан в администрации района",
    student_name="Фамилия Имя Отчество", student_group="324-ИТвБ", course="4",
    study_form="очной", department="прикладной информатики и МЭП",
    direction="38.03.05 Бизнес-информатика", program="Информационные технологии в бизнесе",
    head_name="Фамилия И. О.", head_degree="канд. экон. наук, доцент",
    teacher_name="Фамилия Имя Отчество", teacher_degree="канд. экон. наук",
    teacher_position="доцент", year="2027"), profile="mbi", use_env=False)
r.h1("Реферат", toc=False)
r.volume_line()
r.keywords(["учёт обращений", "документооборот", "BPMN", "автоматизация", "администрация"])
r.text("Объект исследования – процессы учёта обращений граждан …")
r.toc()
r.h1("Введение")
...
r.bib.references()
r.appendix("Форма журнала обращений", "обязательное")
r.save()
```

Наброски без титула: `Report(title_page=False, profile="mbi")`, затем `r.h1("Наброски выпускной квалификационной работы")`, `r.subtitle("для согласования темы с научным руководителем")` (обычное начертание, пустая строка после), абзацы `r.text("…", lead="Тема.")`.

## Школа: индивидуальный проект (стиль файлом)

Требования методички: TNR 12 везде, 1,5; по ширине; красная строка 1 см; поля 2 см; интервалы 0/0; заголовки жирные по центру; номер вверху справа, титул не нумеруется.

```toml
# ~/.config/gost-report/styles/school.toml
extends = "gost"
ministry = ""
toc_title = "Оглавление"
university_full = "Муниципальное бюджетное общеобразовательное учреждение"
university_short = "«Гимназия»"
city = "г. Город"
[page]
margin_left = 20
margin_right = 20
margin_top = 20
margin_bottom = 20
number_position = "top-right"
number_size = 12
[body]
font_size = 12
indent_cm = 1.0
[headings]
space_after_pt = 0
[h1]
size = 12
uppercase = false
[h2]
size = 12
align = "center"
[h3]
size = 12
align = "center"
[title]
margin_left = 20
margin_right = 20
margin_top = 20
margin_bottom = 20
size_large = 12
student_layout = "label_group"
group_template = "{group}"
```

```python
r = Report(TitleConfig(work_type="Индивидуальный проект", topic="Тема проекта",
    student_label="Выполнила", student_group="ученица 9 «Б» класса",
    student_name="Фамилия Имя Отчество", teacher_label="Руководитель",
    teacher_name="Фамилия Имя Отчество", year="2026"), profile="school")
```

## Профиль в Python

Стиль можно собрать и объектом — те же поля:

```python
from gost_report import Report, TitleConfig, UniversityProfile, load_profile

SPBSU = UniversityProfile(
    university_short="«Санкт-Петербургский государственный университет»",
    faculty="Математико-механический факультет",
    city="Санкт-Петербург",
)                                                    # остальное — ГОСТ 7.32
MGU = load_profile("gost", university_short="«МГУ имени М. В. Ломоносова»",
                   h1_uppercase=False, heading_align_h1="left")
```

`TitleConfig` имеет поля `city`, `ministry`, `university_full`, `university_short`, `faculty`: пустые наследуются из профиля, заполненные перекрывают его на одном документе.

## Работа за другого человека

Личные ФИО и группа из `~/.config/gost-report/config` по умолчанию перекрывают `TitleConfig`. Для чужой работы (заказ, сестра): `Report(..., use_env=False)` — титул только из `TitleConfig`, env и конфиги не читаются; или `GOST_REPORT_IGNORE_GLOBAL_CONFIG=1` в `<проект>/.gost-report.env` — отключает только глобальный конфиг.

## Если построил стиль для своего вуза

Положи его в `skills/gost-report/styles/<вуз>.toml` и пришли PR: со временем скилл должен поставляться со стилями всех крупных российских вузов.

## Что менять не нужно

- Не лезь в шрифты, поля, интервалы, подписи через `r.doc` и не наследуйся от `Report` ради кегля или отступа — это поля стиля.
- A4 остаётся инвариантом; `r.doc` — аварийный выход для совсем экзотического (альбомная страница).
