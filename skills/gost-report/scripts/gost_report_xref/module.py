"""XrefModule — namespace r.ref. Фразы-ссылки по ГОСТ 7.32.

    n = r.figure(img, "Схема")              # номер рисунка
    r.text(f"{r.ref.on_figure(n)} показана схема.")   # «На рисунке 3 показана…»
    r.text(f"Результаты сведены {r.ref.in_table(t)}.")# «…в таблице 2.»
    r.text(f"Расчёт ведётся {r.ref.by_formula(f)}.")  # «…по формуле (4).»

Падежные формы — отдельными методами (русский не склоняется одной функцией).
Регистр: cap=True для начала предложения («Рисунок 3 …»). Можно регистрировать
метки: r.ref.set("arch", n) → потом r.ref.on_figure("arch").
"""
from __future__ import annotations

from typing import Dict, Union

Ref = Union[int, str]


class _XrefAPI:
    def __init__(self, core):
        self._core = core
        self._labels: Dict[str, int] = {}

    # ---- регистрация меток (опционально) ----
    def set(self, label: str, number: int) -> int:
        """Связать строковую метку с номером (из r.figure/r.table/r.formula).
        Возвращает номер — удобно: n = r.ref.set('arch', r.figure(...))."""
        self._labels[label] = int(number)
        return int(number)

    def _n(self, ref: Ref) -> int:
        if isinstance(ref, str):
            if ref not in self._labels:
                raise KeyError(
                    f"r.ref: метка {ref!r} не зарегистрирована "
                    f"(сначала r.ref.set({ref!r}, <номер>))")
            return self._labels[ref]
        return int(ref)

    @staticmethod
    def _cap(word: str, cap: bool) -> str:
        return word[:1].upper() + word[1:] if cap else word

    # ---- именительный падеж + «(N)» для формул ----
    def figure(self, ref: Ref, *, cap: bool = False) -> str:
        return f"{self._cap('рисунок', cap)} {self._n(ref)}"

    def table(self, ref: Ref, *, cap: bool = False) -> str:
        return f"{self._cap('таблица', cap)} {self._n(ref)}"

    def formula(self, ref: Ref, *, cap: bool = False) -> str:
        return f"{self._cap('формула', cap)} ({self._n(ref)})"

    def appendix(self, ref: Ref, *, cap: bool = False) -> str:
        return f"{self._cap('приложение', cap)} {ref if isinstance(ref, str) else self._n(ref)}"

    # ---- ходовые предложные/творительные обороты по ГОСТ ----
    def on_figure(self, ref: Ref, *, cap: bool = False) -> str:
        """«на рисунке N»."""
        return f"{self._cap('на', cap)} рисунке {self._n(ref)}"

    def in_table(self, ref: Ref, *, cap: bool = False) -> str:
        """«в таблице N»."""
        return f"{self._cap('в', cap)} таблице {self._n(ref)}"

    def by_formula(self, ref: Ref, *, cap: bool = False) -> str:
        """«по формуле (N)»."""
        return f"{self._cap('по', cap)} формуле ({self._n(ref)})"

    def per_figure(self, ref: Ref, *, cap: bool = False) -> str:
        """ГОСТ-каноничное «в соответствии с рисунком N»."""
        return f"{self._cap('в', cap)} соответствии с рисунком {self._n(ref)}"

    def per_table(self, ref: Ref, *, cap: bool = False) -> str:
        """«в соответствии с таблицей N»."""
        return f"{self._cap('в', cap)} соответствии с таблицей {self._n(ref)}"

    def see_figure(self, ref: Ref) -> str:
        """«(рисунок N)» — для скобочной ссылки."""
        return f"(рисунок {self._n(ref)})"

    def see_table(self, ref: Ref) -> str:
        """«(таблица N)»."""
        return f"(таблица {self._n(ref)})"


class XrefModule:
    namespace = "ref"
    title = "Кросс-ссылки (рисунки/таблицы/формулы)"
    requires_extra = None          # pure-python, default-тир

    def check_available(self) -> None:
        pass

    def attach(self, core):
        return _XrefAPI(core)

    def teardown(self) -> None:
        pass
