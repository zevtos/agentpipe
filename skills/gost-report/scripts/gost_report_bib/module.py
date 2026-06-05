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
from docx.shared import Pt

from .format import format_entry

DEFAULT_TITLE = "Список использованных источников"


class _BibAPI:
    def __init__(self, core):
        self._core = core
        self._entries: Dict[str, dict] = {}
        self._order: List[str] = []          # ключи в порядке первого цитирования
        self._numbers: Dict[str, int] = {}

    def add(self, key: str, **fields) -> str:
        """Зарегистрировать источник. type: book|article|web|conference|standard|
        thesis. Поля: authors, title, city, publisher, year, pages, journal,
        volume, issue, url, accessed, doi, edition. Возвращает key."""
        if key in self._entries:
            raise ValueError(f"r.bib: источник {key!r} уже добавлен")
        self._entries[key] = dict(fields)
        return key

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

    def references(self, title: str = DEFAULT_TITLE,
                   *, include_uncited: bool = False) -> int:
        """Структурный элемент «Список использованных источников»: заголовок
        (h1, в оглавление) + пронумерованные записи ГОСТ Р 7.0.5. Возвращает
        число записей. Если cite() не вызывался — выводит все в порядке add()."""
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
        for i, key in enumerate(ordered, 1):
            entry = format_entry(self._entries[key])
            p = self._core.make_paragraph(
                align=WD_ALIGN_PARAGRAPH.LEFT,
                first_line_indent=Pt(0),
                space_after=Pt(0),
            )
            run = p.add_run(f"{i}. {entry}")     # raw: тире ГОСТ не санируем
            self._core.set_run_font(run)
        return len(ordered)


class BibModule:
    namespace = "bib"
    title = "Список литературы (ГОСТ Р 7.0.5)"
    requires_extra = None          # pure-python, default-тир

    def check_available(self) -> None:
        pass

    def attach(self, core):
        return _BibAPI(core)

    def teardown(self) -> None:
        pass
