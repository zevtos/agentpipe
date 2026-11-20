"""BibModule — namespace r.bib. Список литературы и ссылки по ГОСТ Р 7.0.5.

    r.bib.add("vasiliev2020", type="book", authors=["Васильев А.А."],
              title="Машинное обучение", city="М.", publisher="ДМК Пресс",
              year=2020, pages=420)
    r.bib.add("smith2021", type="article", authors=["Smith J."],
              title="Deep nets", journal="Nature", year=2021,
              volume=5, issue=3, pages="12-18", doi="10.1000/xyz")
    r.bib.add("docs", type="web", authors=["Иванов И.И."], title="Гайд",
              url="https://example.org", accessed="01.06.2026")

    r.text(f"Метод описан в источнике {r.bib.cite('vasiliev2020')}.")  # «… [1].»
    ...
    r.bib.references()   # структурный элемент со списком, нумерация = порядку ссылок

Нумерация источников — по порядку первого цитирования (vancouver-стиль), что
совпадает с номерами в [N]. Если ни одной r.bib.cite() не было — список выводится
в порядке добавления.
"""
from __future__ import annotations

from typing import Dict, List

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt

from .format import format_entry_styled

DEFAULT_TITLE = "Список использованных источников"


class _BibAPI:
    def __init__(self, core):
        self._core = core
        self._entries: Dict[str, dict] = {}
        self._order: List[str] = []          # ключи в порядке первого цитирования
        self._numbers: Dict[str, int] = {}
        self._printed: int = -1              # сколько записей вывел references()

    @property
    def count(self) -> int:
        """Число источников в списке: выведенных references(), иначе
        процитированных (или всех добавленных, если cite() не было)."""
        if self._printed >= 0:
            return self._printed
        return len(self._order) or len(self._entries)

    def add(self, key: str, **fields) -> str:
        """Зарегистрировать источник. type: book|article|web|conference|standard|
        thesis. Поля: authors, title, city, publisher, year, pages, journal,
        volume, issue, url, accessed, doi, edition. Возвращает key."""
        if key in self._entries:
            raise ValueError(f"r.bib: источник {key!r} уже добавлен")
        self._entries[key] = dict(fields)
        return key

    def load(self, path) -> int:
        """Источники из файла .toml / .json: таблица на источник, ключ —
        метка для cite (type, authors, title, … как у add). Относительный
        путь — от корня проекта, docs/ и текущего каталога. Возвращает число
        добавленных источников.

            [ivanov2020]
            type = "book"
            authors = ["Иванов И. И."]
            title = "Название"
        """
        from pathlib import Path
        import styles
        p = Path(path).expanduser()
        if not p.is_absolute():
            paths = self._core.paths
            for base in (paths.root, paths.docs, Path.cwd()):
                if (base / p).is_file():
                    p = base / p
                    break
        data = styles._read_file(p)
        entries = data.get("sources", data)
        if isinstance(entries, list):
            entries = {e["key"]: {k: v for k, v in e.items() if k != "key"} for e in entries}
        for key, fields in entries.items():
            self.add(key, **fields)
        return len(entries)

    def cite(self, *keys: str, cap: bool = False) -> str:
        """Ссылка в тексте → «[1]» / «[1, 2]». Назначает номер при первом
        упоминании (порядок цитирования). cap не влияет (для совместимости API)."""
        if not keys:
            raise ValueError("r.bib.cite: нужен хотя бы один ключ")
        nums = []
        for key in keys:
            if key not in self._entries:
                raise KeyError(
                    f"r.bib.cite: источник {key!r} не добавлен (r.bib.add({key!r}, ...))")
            if key not in self._numbers:
                self._order.append(key)
                self._numbers[key] = len(self._order)
            nums.append(self._numbers[key])
        return "[" + ", ".join(str(n) for n in nums) + "]"

    def number(self, key: str) -> int:
        """Назначенный номер источника (после cite)."""
        return self._numbers[key]

    def references(self, title: str = None,
                   *, include_uncited: bool = False) -> int:
        """Структурный элемент «Список использованных источников»: заголовок
        (h1, в оглавление) + пронумерованные записи. Стиль записи, заголовок,
        выравнивание и отступ — из профиля (bib_style: 7.1-2003 или
        7.0.100-2018, bib_title, bib_align, bib_indent_cm). Возвращает
        число записей. Если cite() не вызывался — выводит все в порядке add()."""
        prof = self._core.profile
        if title is None:
            title = getattr(prof, "bib_title", DEFAULT_TITLE)
        style = getattr(prof, "bib_style", "7.1-2003")
        dash = getattr(prof, "dash", "—")
        align = {"left": WD_ALIGN_PARAGRAPH.LEFT, "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
                 "center": WD_ALIGN_PARAGRAPH.CENTER, "right": WD_ALIGN_PARAGRAPH.RIGHT}.get(
            getattr(prof, "bib_align", "left"), WD_ALIGN_PARAGRAPH.LEFT)
        indent = getattr(prof, "bib_indent_cm", 0)
        if self._order:
            ordered = list(self._order)
            if include_uncited:
                ordered += [k for k in self._entries if k not in self._numbers]
            else:
                uncited = [k for k in self._entries if k not in self._numbers]
                if uncited:
                    import sys
                    sys.stderr.write(
                        "gost-report bib warn: источники добавлены, но не "
                        f"процитированы (пропущены): {', '.join(uncited)}. "
                        "include_uncited=True чтобы включить.\n")
        else:
            # Ни одной ссылки — выводим всё в порядке добавления.
            ordered = list(self._entries)

        self._core.h1(title)
        nbsp = getattr(self._core, "nbsp_only", None)
        for i, key in enumerate(ordered, 1):
            entry = format_entry_styled(self._entries[key], style=style, dash=dash)
            if nbsp is not None:
                entry = nbsp(entry)
            p = self._core.make_paragraph(
                align=align,
                first_line_indent=Cm(indent) if indent else Pt(0),
                space_after=Pt(0),
            )
            # raw: тире ГОСТ не санируем; неразрывные дефисы (ISBN) — элементами
            add_runs = getattr(self._core, "add_text_runs", None)
            if add_runs is not None:
                add_runs(p, f"{i}. {entry}")
            else:
                run = p.add_run(f"{i}. {entry}")
                self._core.set_run_font(run)
        self._printed = len(ordered)
        return len(ordered)


class BibModule:
    namespace = "bib"
    title = "Список литературы (ГОСТ 7.1 / ГОСТ Р 7.0.100)"
    requires_extra = None          # pure-python, default-тир

    def check_available(self) -> None:
        pass

    def attach(self, core):
        return _BibAPI(core)

    def teardown(self) -> None:
        pass
