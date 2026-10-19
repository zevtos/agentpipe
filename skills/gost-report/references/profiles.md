# UniversityProfile — выбор вуза или школы

Разные вузы по-своему трактуют ГОСТ 7.32 (поля страницы, кегли заголовков, «ОГЛАВЛЕНИЕ» vs «СОДЕРЖАНИЕ»), а школьные требования (индивидуальный проект, реферат) часто вообще не ГОСТ: TNR 12, отступ 1 см, номер вверху справа. Профиль фиксирует все эти расхождения. SKILL.md по умолчанию использует ИТМО — для других вузов и школ читай этот файл.

**Методичка с требованиями (ppt/pptx/pdf/docx) → профиль:** извлеки правила (doc2kb; у `.ppt` сначала `soffice --headless --convert-to pptx`), посмотри скриншоты настроек Word на слайдах, затем сопоставь каждое требование полю из таблицы ниже. Чего нет в полях — не правь `r.doc` руками, а добавь поле в `UniversityProfile` (дефолт = текущее поведение, чтобы ИТМО/ГОСТ не поменялись байт-в-байт).

## Встроенные пресеты

| Профиль | Когда |
|---|---|
| `ITMO_PROFILE` (default) | ИТМО, ФПИиКТ. Поля титульника 30/20/10/10 мм, основного текста 30/10/20/20. Кегли заголовков 16/15/14, все по центру. «ОГЛАВЛЕНИЕ». |
| `GOST_PROFILE` | Любой другой вуз, придерживающийся чистого ГОСТ 7.32-2017. Поля 30/15/20/20 мм везде. Кегли 16/14/14, h2/h3 по левому краю. «СОДЕРЖАНИЕ». Поля университета пустые — заполни через `TitleConfig`. |
| Кастомный `UniversityProfile(...)` | Всё остальное. Переопредели только те поля, которые отличаются от `GOST_PROFILE`. |

## Поля `UniversityProfile`

Dataclass со следующими параметрами (все имеют разумные дефолты по ГОСТ 7.32):

```
title_margin_left/right/top/bottom: float (мм)   # поля титульника
body_margin_left/right/top/bottom: float (мм)    # поля основного текста
heading_size_h1/h2/h3: int (пт)                  # кегли заголовков (16/14/14)
heading_align_h1/h2/h3: "center" | "left"
h1_uppercase: bool                               # ВЕРХНИЙ регистр у h1
h1_new_page: bool                                # h1 начинается с новой страницы
toc_title: str                                   # «ОГЛАВЛЕНИЕ» или «СОДЕРЖАНИЕ»
ministry: str                                    # шапка «Министерство...» ("" — нет строки)
university_full: str                             # полное название вуза / школы
university_short: str                            # краткое название «...»
faculty: str                                     # факультет
city: str                                        # город (нижняя строка титульника)

# Типографика (дефолты = ГОСТ 7.32)
body_font_size: float = 14                       # пт, весь текст кроме заголовков
line_spacing: float = 1.5                        # множитель (текст и заголовки)
first_line_indent_cm: float = 1.25               # красная строка
heading_space_after_pt: float = 12               # интервал после заголовков и «ОГЛАВЛЕНИЯ»
block_space_pt: float = 6                        # интервал вокруг рисунков/таблиц/формул
page_number_position: str = "bottom-center"      # "<top|bottom>-<left|center|right>"
page_number_size: float = 11                     # пт

# Титульник
title_size_large: float = 18                     # пт, вид работы и тема
title_right_block_indent_cm: float = 9           # сдвиг блока «Выполнил/Проверил»
title_student_layout: str = "group_line"         # или "label_group" (школа)
title_group_template: str = "Группа: {group}"    # {group} ← TitleConfig.student_group
title_builder: callable(report) | None           # свой титульник целиком
```

Номер страницы на титуле всегда скрыт («особый колонтитул для первой страницы»), со второй страницы (оглавление) виден. Опечатка в `page_number_position` / `title_student_layout` — `ValueError` при создании `Report`.

**`title_student_layout`:**
- `"group_line"` (вуз): строка «Группа: P3XXX» (подчёркнута), ниже «Выполнил: ФИО».
- `"label_group"` (школа): «<u>Выполнила</u>: ученица 9 «Б» класса», ФИО строками ниже под меткой — как у руководителя. С `title_group_template="{group}"` в `student_group` пишется вся фраза («ученица 9 «Б» класса»).

**`title_builder`** — когда раскладка совсем своя. Секция, поля и скрытый номер уже настроены; внутри доступны `report.title_config`, `report.add_text_paragraph(text, align=, size=, bold=, ...)`, `report.add_runs_paragraph([StyledRun(...)], align=, left_indent=)`, `report.add_blank_paragraphs(n)`, `report.add_bottom_block([строки])` (прижато к нижнему полю).

## Минимальный кастомный профиль

```python
from gost_report import Report, TitleConfig, UniversityProfile

SPBSU_PROFILE = UniversityProfile(
    university_short="«Санкт-Петербургский государственный университет»",
    faculty="Математико-механический факультет",
    city="Санкт-Петербург",
    # Остальные поля наследуют дефолты ГОСТ 7.32
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

## Профиль с особым форматированием

Если у вуза есть нестандартные требования (например, h1 без верхнего регистра, шире поля, другие кегли):

```python
MGU_PROFILE = UniversityProfile(
    university_short="«Московский государственный университет имени М.В. Ломоносова»",
    faculty="Физический факультет",
    city="Москва",
    body_margin_left=25, body_margin_right=15,    # уже левое поле
    heading_size_h1=14,                           # h1 поменьше
    heading_align_h1="left",                      # h1 не центруется
    h1_uppercase=False,                           # без КАПСЛОКА
    toc_title="СОДЕРЖАНИЕ",
)
```

## Школа: индивидуальный проект (реальный пример)

МБОУ «Ломоносовская гимназия», Петрозаводск, 9 класс. Требования методички: TNR 12 везде, 1,5; по ширине; красная строка 1 см; поля 2 см; интервалы 0/0; заголовки жирные по центру; номер вверху справа, титул не нумеруется.

```python
from gost_report import Report, TitleConfig, UniversityProfile

SCHOOL = UniversityProfile(
    title_margin_left=20, title_margin_right=20, title_margin_top=20, title_margin_bottom=20,
    body_margin_left=20, body_margin_right=20, body_margin_top=20, body_margin_bottom=20,
    body_font_size=12, first_line_indent_cm=1.0, heading_space_after_pt=0,
    heading_size_h1=12, heading_size_h2=12, heading_size_h3=12,
    heading_align_h2="center", heading_align_h3="center", h1_uppercase=False,
    page_number_position="top-right", page_number_size=12, title_size_large=12,
    title_student_layout="label_group", title_group_template="{group}",
    toc_title="Оглавление", ministry="",
    university_full="Муниципальное бюджетное общеобразовательное учреждение "
                    "Петрозаводского городского округа",
    university_short="«Ломоносовская гимназия»", city="г. Петрозаводск",
)
r = Report(TitleConfig(
    work_type="Индивидуальный проект", topic="Тема проекта",
    student_label="Выполнила", student_group="ученица 9 «Б» класса",
    student_name="Фамилия Имя Отчество",
    teacher_label="Руководитель", teacher_name="Фамилия Имя Отчество", year="2026",
), profile=SCHOOL)
r.toc()
r.h1("Введение")
r.h1("Глава 1. Теоретическая часть")
r.h2("§1. Название параграфа")
```

**Работа за другого человека** (сестра, заказ): положи рядом `.gost-report.env` с `GOST_REPORT_IGNORE_GLOBAL_CONFIG=1` — иначе ФИО/группа из твоего `~/.config/gost-report/config` перебьют `TitleConfig` на чужом титуле.

## TitleConfig: переопределение полей профиля

`TitleConfig` имеет те же поля, что и `UniversityProfile` (`city`, `ministry`, `university_full`, `university_short`, `faculty`). Они **пустые по умолчанию** и наследуют значение из активного профиля. Заполнять стоит только если на этом конкретном документе нужно что-то отличающееся (например, у одного факультета вуза специальное название):

```python
r = Report(
    TitleConfig(
        work_type="Лабораторная работа",
        topic="...",
        student_name="...",
        student_group="...",
        year="2026",
        faculty="Инженерно-физический институт",  # переопределяет profile.faculty
    ),
    profile=SPBSU_PROFILE,
)
```

## Если построил профиль для своего вуза

Пришли его как PR в репозиторий. Со временем скилл должен поставляться с пресетами для всех крупных российских вузов.

## Что менять не нужно

- Не лезь руками в шрифты/поля через `r.doc` для типичных случаев — собери `UniversityProfile`.
- Кегль, межстрочный интервал, абзацный отступ, интервалы после заголовков и позиция номера страницы — поля профиля (см. выше), не правь их через `r.doc`. Шрифт Times New Roman и A4 остаются инвариантами.
