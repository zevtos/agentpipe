"""markdown_input — текст работы в Markdown, оформление — из профиля.

    r = Report(TitleConfig(...), profile="mbi")
    r.bib.load("sources.toml")          # источники файлом
    r.include("vkr.md")                 # или r.markdown(строка)
    r.save()

Синтаксис (подмножество Markdown + команды в стиле LaTeX, каждая с новой строки):

    # Введение                      → r.h1; ## → h2; ### → h3
    # Реферат {.notoc}              без строки в оглавлении; {-} — без номера
    Абзац текста, **жирный**, *курсив*, $x^2$, ссылка [@ivanov2020; @gost7_32].
    **Тема.** Абзац с жирным началом.
    **Глава 2.** Проектирование (14–17 с.) {.keep}   не отрывать от следующего блока;
                                    также {.noindent}, {align=center}
    - пункт / * пункт / – пункт     → r.bullet; 1. / 1) → r.numbered; а) → r.lettered
      1) вложенный (отступ 2+ пробела)
    > Примечание к тексту           → r.note; несколько абзацев «>» → «Примечания:»
    ![Схема процесса](fig/scheme.png){#fig:scheme width=14}
    Таблица: Этапы и сроки {#tbl:stages widths=6,3,7 note="Сроки по закону"}
    | Этап | Срок, дн. |
    |:-----|:---------:|              выравнивание столбцов — двоеточиями
    | Регистрация | 1 |
    $$ \\rho = \\frac{m}{V} $$ {#eq:rho}
    где $m$ – масса, кг; $V$ – объём, м³   (абзац сразу после формулы)
    ```                              → r.code
    \\toc  \\newpage  \\blank{2}  \\volume  \\keywords{а, б, в}  \\subtitle{…}
    \\appendix[справочное]{Название}  \\references

Ссылки на рисунки, таблицы, формулы и приложения — по метке, в том числе
вперёд (номер подставляется в r.save()): «на рисунке @fig:scheme»,
«в таблице @tbl:stages», «по формуле (@eq:rho)». \\@ — литеральная «@».
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HEADING = re.compile(r"^(#{1,3})\s+(.*?)\s*(?:\{([^{}]*)\})?\s*$")
_DIRECTIVE = re.compile(r"^\\([A-Za-z]+)(?:\[([^\]]*)\])?(?:\{(.*)\})?\s*$")
_IMAGE = re.compile(r"^!\[(.*?)\]\((.+?)\)\s*(?:\{([^{}]*)\})?\s*$")
_CAPTION = re.compile(r"^(?:Таблица|Table)\s*:\s*(.*?)\s*(?:\{([^{}]*)\})?\s*$")
_LIST_ITEM = re.compile(r"^(\s*)([-*–•]|\d+[.)]|[а-яё]\))\s+(.*)$")
_CITE = re.compile(r"\[(@[\w:.-]+(?:\s*;\s*@[\w:.-]+)*)\]")
_REF = re.compile(r"(?<![\\\w])@((?:fig|tbl|eq|app):[\w-]+(?:\.[\w-]+)*)")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _attrs(raw: Optional[str]) -> Dict[str, Any]:
    """«#fig:x width=14 .notoc note="…"» → {"id": "fig:x", "width": "14", ...}."""
    out: Dict[str, Any] = {}
    if not raw:
        return out
    for tok in shlex.split(raw):
        if tok.startswith("#"):
            out["id"] = tok[1:]
        elif tok.startswith("."):
            out[tok[1:]] = True
        elif tok == "-":
            out["unnumbered"] = True
        elif "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
        else:
            out[tok] = True
    return out


def _list_kind(marker: str) -> str:
    if marker[0].isdigit():
        return "numbered"
    if marker.endswith(")"):
        return "lettered"
    return "bullet"


class _Renderer:
    def __init__(self, report, base_dir: Optional[Path]):
        self.r = report
        self.base = base_dir

    # ---- inline ----
    def inline(self, text: str) -> str:
        r = self.r

        def cite(m: "re.Match[str]") -> str:
            keys = [k.strip()[1:] for k in m.group(1).split(";")]
            return r.bib.cite(*keys)

        text = _CITE.sub(cite, text)
        text = _REF.sub(lambda m: r.deferred_ref(m.group(1)), text)
        return text.replace("\\@", "@")

    # ---- blocks ----
    def run(self, text: str) -> None:
        lines = text.replace("\r\n", "\n").split("\n")
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                i += 1
                continue
            if stripped.startswith("```"):
                body = []
                i += 1
                while i < n and not lines[i].strip().startswith("```"):
                    body.append(lines[i])
                    i += 1
                self.r.code("\n".join(body))
                i += 1
                continue
            if stripped.startswith("$$"):
                i = self._math(lines, i)
                continue
            m = _HEADING.match(stripped)
            if m:
                self._heading(len(m.group(1)), m.group(2), _attrs(m.group(3)))
                i += 1
                continue
            m = _DIRECTIVE.match(stripped)
            if m:
                self._directive(m.group(1), m.group(2), m.group(3))
                i += 1
                continue
            m = _IMAGE.match(stripped)
            if m:
                self._image(m.group(1), m.group(2), _attrs(m.group(3)))
                i += 1
                continue
            cap = _CAPTION.match(stripped)
            if cap and i + 1 < n and lines[i + 1].lstrip().startswith("|"):
                i = self._table(lines, i + 1, cap.group(1), _attrs(cap.group(2)))
                continue
            if stripped.startswith("|"):
                i = self._table(lines, i, None, {})
                continue
            if _LIST_ITEM.match(line) and not stripped.startswith("**"):
                i = self._list(lines, i)
                continue
            if stripped.startswith(">"):
                i = self._quote(lines, i)
                continue
            i = self._paragraph(lines, i)

    def _paragraph(self, lines: List[str], i: int) -> int:
        buf = []
        while i < len(lines) and lines[i].strip() and not self._starts_block(lines[i]):
            buf.append(lines[i].strip())
            i += 1
        if buf:
            text, kw = _paragraph_attrs(" ".join(buf))
            self.r.text(self.inline(text), markup=True, **kw)
        return i

    @staticmethod
    def _starts_block(line: str) -> bool:
        """Строка начинает новый блок, а не продолжает абзац."""
        s = line.strip()
        if s.startswith(("```", "$$", ">", "|")) or _HEADING.match(s):
            return True
        if s.startswith("\\") and _DIRECTIVE.match(s):
            return True
        if _IMAGE.match(s):
            return True
        return bool(_LIST_ITEM.match(line)) and not s.startswith("**")

    def _heading(self, level: int, text: str, attrs: Dict[str, Any]) -> None:
        kw: Dict[str, Any] = {}
        if attrs.get("unnumbered"):
            kw["numbered"] = False
        if attrs.get("notoc"):
            kw["toc"] = False
        getattr(self.r, f"h{level}")(self.inline(text), **kw)

    def _directive(self, name: str, opt: Optional[str], arg: Optional[str]) -> None:
        r = self.r
        name = name.lower()
        if name in ("toc", "tableofcontents"):
            r.toc()
        elif name in ("newpage", "pagebreak", "clearpage"):
            r.page_break()
        elif name == "blank":
            r.blank(int(arg or 1))
        elif name == "volume":
            r.volume_line()
        elif name == "keywords":
            r.keywords(arg or "")
        elif name == "subtitle":
            r.subtitle((arg or "").replace("\\\\", "\n"))
        elif name == "appendix":
            label = None
            title = arg or ""
            m = re.match(r"^(.*?)\s*\{#([\w:.-]+)\}\s*$", title)
            if m:
                title, label = m.group(1), m.group(2)
            letter = r.appendix(title, opt if opt is not None else "обязательное")
            if label:
                r.set_label(label, letter)
        elif name in ("references", "bibliography"):
            r.bib.references(arg) if arg else r.bib.references()
        else:
            raise ValueError(f"markdown: неизвестная команда \\{name}")

    def _resolve(self, path: str) -> Any:
        p = Path(path)
        if not p.is_absolute() and self.base is not None and (self.base / p).exists():
            return self.base / p
        return path

    def _image(self, caption: str, path: str, attrs: Dict[str, Any]) -> None:
        width = float(attrs["width"]) if "width" in attrs else None
        num = self.r.figure(self._resolve(path), self.inline(caption), width_cm=width,
                            explication=attrs.get("explication"))
        if attrs.get("id"):
            self.r.set_label(attrs["id"], num)

    def _math(self, lines: List[str], i: int) -> int:
        first = lines[i].strip()[2:]
        body: List[str] = []
        tail = ""
        if "$$" in first:
            body.append(first[: first.index("$$")])
            tail = first[first.index("$$") + 2:]
            i += 1
        else:
            body.append(first)
            i += 1
            while i < len(lines) and "$$" not in lines[i]:
                body.append(lines[i])
                i += 1
            if i < len(lines):
                last = lines[i]
                body.append(last[: last.index("$$")])
                tail = last[last.index("$$") + 2:]
                i += 1
        attrs = _attrs(tail.strip().strip("{}")) if tail.strip() else {}
        j = i
        while j < len(lines) and not lines[j].strip():
            j += 1
        where = None
        if j < len(lines) and lines[j].strip().lower().startswith("где "):
            buf = []
            while j < len(lines) and lines[j].strip() and not self._starts_block(lines[j]):
                buf.append(lines[j].strip())
                j += 1
            where = _where_pairs(self.inline(" ".join(buf)[4:]))
            i = j
        num = self.r.formula(" ".join(x.strip() for x in body if x.strip()), where=where)
        if attrs.get("id"):
            self.r.set_label(attrs["id"], num)
        return i

    def _split_row(self, line: str) -> List[str]:
        s = line.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|") and not s.endswith("\\|"):
            s = s[:-1]
        cells = re.split(r"(?<!\\)\|", s)
        return [c.strip().replace("\\|", "|") for c in cells]

    def _table(self, lines: List[str], i: int, caption: Optional[str],
               attrs: Dict[str, Any]) -> int:
        rows: List[List[str]] = []
        aligns: Optional[List[Optional[str]]] = None
        header = False
        while i < len(lines) and lines[i].lstrip().startswith("|"):
            line = lines[i]
            if _TABLE_SEP.match(line.strip()):
                header = True
                aligns = []
                for cell in self._split_row(line):
                    c = cell.strip()
                    if c.startswith(":") and c.endswith(":"):
                        aligns.append("center")
                    elif c.endswith(":"):
                        aligns.append("right")
                    elif c.startswith(":"):
                        aligns.append("left")
                    else:
                        aligns.append(None)
            else:
                rows.append([self.inline(c) for c in self._split_row(line)])
            i += 1
        if caption is None and i < len(lines):
            cap = _CAPTION.match(lines[i].strip())
            if cap:
                caption, attrs = cap.group(1), _attrs(cap.group(2))
                i += 1
        kw: Dict[str, Any] = {"has_header": header and not attrs.get("noheader")}
        if attrs.get("widths"):
            kw["widths"] = [float(w) for w in str(attrs["widths"]).split(",")]
        if attrs.get("align"):
            kw["align"] = attrs["align"]
        elif aligns and any(aligns):
            kw["align"] = aligns
        if attrs.get("note"):
            kw["note"] = self.inline(attrs["note"])
        num = self.r.table(rows, caption=self.inline(caption or ""), **kw)
        if attrs.get("id"):
            self.r.set_label(attrs["id"], num)
        return i

    def _list(self, lines: List[str], i: int) -> int:
        items: List[Tuple[int, str, str]] = []      # (отступ, маркер, текст)
        while i < len(lines):
            m = _LIST_ITEM.match(lines[i])
            if m and not lines[i].strip().startswith("**"):
                items.append((len(m.group(1).expandtabs(4)), m.group(2), m.group(3).strip()))
                i += 1
                continue
            if lines[i].strip() and items and lines[i].startswith((" ", "\t")):
                ind, mk, txt = items[-1]
                items[-1] = (ind, mk, txt + " " + lines[i].strip())
                i += 1
                continue
            break
        tree, kinds = self._nest(items, 0, items[0][0] if items else 0)
        kind = _list_kind(items[0][1]) if items else "bullet"
        nested = kinds or ("numbered" if kind != "numbered" else "bullet")
        getattr(self.r, {"bullet": "bullet", "numbered": "numbered",
                         "lettered": "lettered"}[kind])(tree, nested=nested)
        return i

    def _nest(self, items, pos, indent):
        out: List[Any] = []
        nested_kind = None
        k = pos
        while k < len(items):
            ind, mk, txt = items[k]
            if ind < indent:
                break
            if ind > indent and out:
                sub_items = []
                j = k
                while j < len(items) and items[j][0] > indent:
                    sub_items.append(items[j])
                    j += 1
                sub, _ = self._nest(sub_items, 0, sub_items[0][0])
                nested_kind = nested_kind or _list_kind(sub_items[0][1])
                prev = out[-1]
                text = prev[0] if isinstance(prev, tuple) else prev
                out[-1] = (text, sub)
                k = j
                continue
            out.append(self.inline(txt))
            k += 1
        self._last_nested = nested_kind
        return out, nested_kind

    def _quote(self, lines: List[str], i: int) -> int:
        paras: List[str] = []
        buf: List[str] = []
        while i < len(lines) and lines[i].strip().startswith(">"):
            s = lines[i].strip()[1:].strip()
            if s:
                buf.append(s)
            elif buf:
                paras.append(" ".join(buf))
                buf = []
            i += 1
        if buf:
            paras.append(" ".join(buf))
        paras = [self.inline(p) for p in paras]
        self.r.note(paras[0] if len(paras) == 1 else paras)
        return i


_PARA_ATTRS = re.compile(r"\s*\{([^{}]*)\}\s*$")


def _paragraph_attrs(text: str):
    """Атрибуты абзаца в конце: «… {.keep}» — не отрывать от следующего блока,
    {.noindent} — без абзацного отступа, {align=center}. Фигурные скобки с
    чем-то другим остаются текстом."""
    m = _PARA_ATTRS.search(text)
    if not m:
        return text, {}
    kw: Dict[str, Any] = {}
    for key, value in _attrs(m.group(1)).items():
        if key in ("keep", "keepnext", "keep_with_next") and value is True:
            kw["keep_with_next"] = True
        elif key == "noindent" and value is True:
            kw["indent"] = False
        elif key == "align" and value in ("left", "center", "right", "justify"):
            kw["align"] = value
        else:
            return text, {}
    return text[:m.start()].rstrip(), kw


_WHERE_ITEM = re.compile(r"^\s*(\$[^$]+\$|\S+)\s+[–—-]\s+(.+?)\s*[;.]?\s*$")


def _where_pairs(text: str):
    """«$m$ – масса, кг; $V$ – объём, м³» → [("$m$", "масса, кг"), …]: каждый
    символ с новой строки (ГОСТ 7.32, 2.105). Не разобралось — строка как есть."""
    parts = [p for p in re.split(r";\s*", text.strip().rstrip(".")) if p.strip()]
    pairs = []
    for part in parts:
        m = _WHERE_ITEM.match(part)
        if not m:
            return text
        pairs.append((m.group(1), m.group(2)))
    return pairs if len(pairs) > 1 else text


def render(report, text: str, *, base_dir: Optional[Path] = None) -> None:
    _Renderer(report, base_dir).run(text)
