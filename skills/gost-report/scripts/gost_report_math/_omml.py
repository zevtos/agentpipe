"""gost_report_math._omml — LaTeX → OMML конвертация (Office Math).

Вынесено из монолита gost_report.py без изменения логики (behavior-preserving,
проверено golden-master тестами). Чистые функции: ни состояния Report, ни
зависимостей кроме docx OxmlElement/qn и stdlib ElementTree.

Публичная точка входа — latex_to_omath(latex) → <m:oMath>.
"""
from __future__ import annotations

import xml.etree.ElementTree as _ET
from typing import List, Optional

from docx.oxml import OxmlElement
from docx.oxml.ns import qn


# ============================================================
# LaTeX → OMML
# ============================================================
#
# Word хранит формулы в OMML (Office Math, namespace
# http://schemas.openxmlformats.org/officeDocument/2006/math, prefix m:),
# а не в MathML. Поэтому путь такой:
#
#     LaTeX  --[latex2mathml]-->  MathML  --[наш walker]-->  OMML
#
# latex2mathml делает самую противную часть (нерегулярная грамматика LaTeX),
# а MathML→OMML это прямой обход дерева: теги мапятся почти 1:1.
#
# Сама строка LaTeX **не** проходит через _sanitize_prose — иначе
# `\text{1—5}` или `[a—b]` сломались бы. Санируется только пользовательская
# проза в where=... (это обычный русский текст, к нему применимы общие
# правила: никаких длинных тире).

_MATHML_NS = "http://www.w3.org/1998/Math/MathML"
_M_PREFIX = "{" + _MATHML_NS + "}"

# Реляционные операторы — границы тела N-ary (после `=`, `<`, `≥` и т.п.
# идёт уже не подынтегральное выражение, а вторая часть равенства). При
# сборе body для <m:e> упираемся в эти символы и останавливаемся.
_NARY_BODY_TERMINATORS = {
    "=", "<", ">", "≤", "≥", "≠", "≈", "≡",
    "≪", "≫", "⇔", "⇒", "↔", "→", "←", "∝",
}


# N-ary операторы: символ → размещение пределов. undOvr = пределы
# сверху/снизу (∑, ∏), subSup = справа от знака (∫, ∮).
_NARY_OPS = {
    "∑": "undOvr",  # ∑
    "∏": "undOvr",  # ∏
    "∐": "undOvr",  # ∐
    "⋃": "undOvr",  # ⋃
    "⋂": "undOvr",  # ⋂
    "⨂": "undOvr",  # ⨂
    "⨁": "undOvr",  # ⨁
    "⨀": "undOvr",  # ⨀
    "∫": "subSup",  # ∫
    "∬": "subSup",  # ∬
    "∭": "subSup",  # ∭
    "∮": "subSup",  # ∮
}

# Акценты: \bar, \hat, \vec, \tilde, \dot, \ddot, \check, \acute, \grave, \breve.
# latex2mathml выдаёт spacing-формы (¯ ^ ~ ¨ ´ ` ˘ ˙ ˇ →), но Word в <m:acc>
# умеет красиво рисовать только combining-формы (U+0300-U+030C, U+20D7).
# Spacing-знак рисуется у baseline и пересекает букву; combining-знак — сверху.
# При эмите OMML заменяем spacing → combining через _ACCENT_NORMALIZE.

_ACCENT_NORMALIZE = {
    "¯": "̄",  # MACRON → COMBINING MACRON (\bar)
    "^": "̂",  # CIRCUMFLEX → COMBINING CIRCUMFLEX (\hat)
    "~": "̃",  # TILDE → COMBINING TILDE (\tilde)
    "¨": "̈",  # DIAERESIS → COMBINING DIAERESIS (\ddot)
    "´": "́",  # ACUTE → COMBINING ACUTE (\acute)
    "`": "̀",  # GRAVE → COMBINING GRAVE (\grave)
    "˘": "̆",  # BREVE → COMBINING BREVE (\breve)
    "˙": "̇",  # DOT ABOVE → COMBINING DOT ABOVE (\dot)
    "ˇ": "̌",  # CARON → COMBINING CARON (\check)
    "→": "⃗",  # RIGHTWARDS ARROW → COMBINING RIGHT ARROW ABOVE (\vec)
}

_ACCENT_CHARS = set(_ACCENT_NORMALIZE.keys()) | set(_ACCENT_NORMALIZE.values())


def _ml_local(tag: str) -> str:
    """Имя MathML-тега без namespace-префикса."""
    if tag.startswith(_M_PREFIX):
        return tag[len(_M_PREFIX):]
    return tag


def _omml(tag: str) -> OxmlElement:
    """OMML-элемент с префиксом m: (math namespace зарегистрирован в python-docx)."""
    return OxmlElement(f"m:{tag}")


def _set_mval(el: OxmlElement, val: str) -> None:
    el.set(qn("m:val"), val)


def _omml_run(text: str, *, plain: bool = False) -> OxmlElement:
    """<m:r>[<m:rPr><m:sty m:val="p"/></m:rPr>]<m:t>text</m:t></m:r>.

    plain=True ставит «прямой» стиль (для чисел, операторов, многобуквенных
    идентификаторов вроде sin/log). Для одиночных букв — курсив (дефолт OMML).
    """
    r = _omml("r")
    if plain:
        rPr = _omml("rPr")
        sty = _omml("sty")
        _set_mval(sty, "p")
        rPr.append(sty)
        r.append(rPr)
    t = _omml("t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    r.append(t)
    return r


def _omml_wrap(tag: str, children: List[OxmlElement]) -> OxmlElement:
    """<m:tag>...children...</m:tag>"""
    wrapper = _omml(tag)
    for c in children:
        wrapper.append(c)
    return wrapper


def _is_nary_op(node) -> Optional[str]:
    """Вернёт N-ary символ если узел — это <mo>∑</mo> / <mo>∫</mo> и т.п."""
    if _ml_local(node.tag) != "mo":
        return None
    text = (node.text or "").strip()
    return text if text in _NARY_OPS else None


def _build_nary(chr_text: str,
                sub_children: List[OxmlElement],
                sup_children: List[OxmlElement],
                *,
                e_children: Optional[List[OxmlElement]] = None,
                hide_sub: bool = False,
                hide_sup: bool = False) -> OxmlElement:
    """<m:nary> для N-ary оператора с пределами и подынтегральным <m:e>.

    e_children — содержимое тела (что идёт после знака суммы/интеграла).
    Если None или пустой список, Word нарисует placeholder-квадрат на месте
    тела — поэтому собирать body нужно на уровне mrow с помощью lookahead.
    """
    nary = _omml("nary")
    naryPr = _omml("naryPr")
    chr_el = _omml("chr")
    _set_mval(chr_el, chr_text)
    naryPr.append(chr_el)
    limLoc = _omml("limLoc")
    _set_mval(limLoc, _NARY_OPS.get(chr_text, "subSup"))
    naryPr.append(limLoc)
    if hide_sub:
        sh = _omml("subHide")
        _set_mval(sh, "1")
        naryPr.append(sh)
    if hide_sup:
        sh = _omml("supHide")
        _set_mval(sh, "1")
        naryPr.append(sh)
    nary.append(naryPr)
    nary.append(_omml_wrap("sub", sub_children))
    nary.append(_omml_wrap("sup", sup_children))
    nary.append(_omml_wrap("e", e_children or []))
    return nary


def _extract_nary_info(node):
    """Если node это msub/msup/msubsup/munder/mover/munderover, базой которой
    является N-ary оператор (∑, ∫, ∏, …) — вернёт кортеж
    (chr_text, sub_children, sup_children, hide_sub, hide_sup).
    Иначе None.
    """
    tag = _ml_local(node.tag)
    kids = list(node)
    if tag in ("msup", "mover") and len(kids) >= 2:
        chr_text = _is_nary_op(kids[0])
        if chr_text:
            return (chr_text, [], _walk_mathml(kids[1]), True, False)
    if tag in ("msub", "munder") and len(kids) >= 2:
        chr_text = _is_nary_op(kids[0])
        if chr_text:
            return (chr_text, _walk_mathml(kids[1]), [], False, True)
    if tag in ("msubsup", "munderover") and len(kids) >= 3:
        chr_text = _is_nary_op(kids[0])
        if chr_text:
            return (chr_text,
                    _walk_mathml(kids[1]),
                    _walk_mathml(kids[2]),
                    False, False)
    return None


def _is_body_terminator(node) -> bool:
    """Останавливает сбор body N-ary оператора. Реляционные операторы (=, ≤,
    ⇒ и т.п.) разделяют интегранд от правой части уравнения."""
    if _ml_local(node.tag) != "mo":
        return False
    text = (node.text or "").strip()
    return text in _NARY_BODY_TERMINATORS


def _walk_with_nary(children, start: int, *, stop_at_terminator: bool):
    """Обход списка MathML-детей с lookahead для N-ary операторов.

    Возвращает (omml_elements, next_index) — индекс на первый элемент,
    который не был поглощён (терминатор либо конец списка).

    stop_at_terminator=True: останавливаемся при первом теле-терминаторе
        (=, ≤, ≠, …) и НЕ съедаем его — он остаётся для caller'а. Это
        режим сбора body внутри N-ary: правая часть равенства не
        принадлежит подынтегральному выражению.
    stop_at_terminator=False: терминаторы рендерятся как обычные mo,
        потребляются. Режим для верхнего mrow.

    Рекурсивный — вложенные N-ary (∑∑..., ∫∑..., etc.) обрабатываются
    корректно: внутренний оператор поглощает свой body раньше, чем
    внешний продолжает сбор.
    """
    result: List[OxmlElement] = []
    j = start
    while j < len(children):
        child = children[j]
        if stop_at_terminator and _is_body_terminator(child):
            break
        nary_info = _extract_nary_info(child)
        if nary_info:
            chr_text, sub_kids, sup_kids, hide_sub, hide_sup = nary_info
            inner_body, next_j = _walk_with_nary(
                children, j + 1, stop_at_terminator=True
            )
            result.append(_build_nary(
                chr_text, sub_kids, sup_kids,
                e_children=inner_body,
                hide_sub=hide_sub, hide_sup=hide_sup,
            ))
            j = next_j
            continue
        result.extend(_walk_mathml(child))
        j += 1
    return result, j


def _build_mtable(table_node, *, left_align: bool) -> OxmlElement:
    """MathML <mtable> → OMML <m:m>. Если left_align=True, ставим левое
    выравнивание колонок (нужно для cases, где обе ветви прижимаются влево)."""
    m_el = _omml("m")
    rows = [r for r in table_node if _ml_local(r.tag) == "mtr"]
    col_count = 0
    for row_node in rows:
        cells = [c for c in row_node if _ml_local(c.tag) == "mtd"]
        if len(cells) > col_count:
            col_count = len(cells)
    if left_align and col_count > 0:
        mPr = _omml("mPr")
        mcs = _omml("mcs")
        mc = _omml("mc")
        mcPr = _omml("mcPr")
        cnt = _omml("count")
        _set_mval(cnt, str(col_count))
        mcPr.append(cnt)
        jc = _omml("mcJc")
        _set_mval(jc, "left")
        mcPr.append(jc)
        mc.append(mcPr)
        mcs.append(mc)
        mPr.append(mcs)
        m_el.append(mPr)
    for row_node in rows:
        mr = _omml("mr")
        for cell_node in row_node:
            if _ml_local(cell_node.tag) != "mtd":
                continue
            cell_kids: List[OxmlElement] = []
            for child in cell_node:
                cell_kids.extend(_walk_mathml(child))
            mr.append(_omml_wrap("e", cell_kids))
        m_el.append(mr)
    return m_el


_OPEN_BRACKETS = {"(", "[", "{", "|", "‖", "⟨", "⌊", "⌈"}
_CLOSE_BRACKETS = {")", "]", "}", "|", "‖", "⟩", "⌋", "⌉"}


def _is_fence_mo(node, *, side: str) -> bool:
    """Проверка, что mo-узел работает как скобка (stretchy fence или
    обычный текстовый символ одной из открывающих/закрывающих скобок).
    side: "open" или "close".
    """
    if _ml_local(node.tag) != "mo":
        return False
    if node.get("stretchy") == "true" and node.get("fence") == "true":
        return True
    text = (node.text or "").strip()
    if side == "open":
        return text in _OPEN_BRACKETS
    return text in _CLOSE_BRACKETS


def _try_fenced_mtable(children) -> Optional[List[OxmlElement]]:
    """Распознать паттерн \\begin{cases}, \\begin{pmatrix} и аналоги (matrix,
    обёрнутая скобками) в children MathML mrow и собрать OMML <m:d>+<m:m>
    с правильно растягивающимися скобками.

    latex2mathml сериализует:
      * cases / \\left\\{ ... \\right. — <mo stretchy fence prefix>{</mo>
        <mtable>...</mtable> [<mo stretchy fence postfix/>]
      * pmatrix / bmatrix — <mo>(</mo><mtable>...</mtable><mo>)</mo>
        (без stretchy/fence-атрибутов, простые символы скобок)

    Голая <mtable> без скобок обрабатывается обычным mtable-handler'ом.
    Возвращает список OMML-элементов либо None, если паттерн не совпал.
    """
    # Найти позицию первого open-fence + mtable подряд
    open_idx = None
    for i in range(len(children) - 1):
        if (
            _is_fence_mo(children[i], side="open")
            and _ml_local(children[i + 1].tag) == "mtable"
        ):
            open_idx = i
            break
    if open_idx is None:
        return None

    open_char = (children[open_idx].text or "").strip() or "{"
    table_node = children[open_idx + 1]
    close_char = ""
    consumed = open_idx + 2
    if consumed < len(children) and _is_fence_mo(children[consumed], side="close"):
        close_char = (children[consumed].text or "").strip()
        consumed += 1

    # Cases-стиль (без правой скобки или непарные скобки) выравниваем влево;
    # обычные pmatrix/bmatrix оставляем с дефолтным центрированием.
    is_cases_like = close_char == "" or (open_char == "{" and close_char != "}")
    m_el = _build_mtable(table_node, left_align=is_cases_like)

    d = _omml("d")
    dPr = _omml("dPr")
    beg = _omml("begChr")
    _set_mval(beg, open_char)
    dPr.append(beg)
    end = _omml("endChr")
    _set_mval(end, close_char)
    dPr.append(end)
    d.append(dPr)
    d.append(_omml_wrap("e", [m_el]))

    result: List[OxmlElement] = []
    for k in range(open_idx):
        result.extend(_walk_mathml(children[k]))
    result.append(d)
    for k in range(consumed, len(children)):
        result.extend(_walk_mathml(children[k]))
    return result


def _handle_container(node) -> List[OxmlElement]:
    """math/mstyle/mrow/semantics/annotation: плющим в плоский список
    с lookahead для N-ary тел и попыткой распознать fenced mtable."""
    children = [c for c in node if _ml_local(c.tag) != "annotation"]
    cases = _try_fenced_mtable(children)
    if cases is not None:
        return cases
    result, _ = _walk_with_nary(children, 0, stop_at_terminator=False)
    return result


def _handle_mi(node) -> List[OxmlElement]:
    text = (node.text or "").strip()
    if not text:
        return []
    # Многобуквенные идентификаторы (sin, log, lim, exp) — прямые;
    # одиночные буквы (включая греческие) — курсив (дефолт OMML).
    # mathvariant="normal" принудительно делает прямой шрифт.
    plain = len(text) > 1 or node.get("mathvariant") == "normal"
    return [_omml_run(text, plain=plain)]


def _handle_simple_text(node) -> List[OxmlElement]:
    """mn/mo/mtext: одинаковая логика, разные пустые-кейсы для mtext."""
    tag = _ml_local(node.tag)
    text = node.text or ""
    if not text or not text.strip():
        # Пустые mo (вокруг скобок и т.п.) — часто нерелевантны
        if tag == "mtext":
            return [_omml_run(text, plain=True)] if text else []
        return []
    return [_omml_run(text, plain=True)]


def _handle_mspace(node) -> List[OxmlElement]:
    return [_omml_run(" ", plain=True)]


def _handle_mfrac(node) -> List[OxmlElement]:
    kids = list(node)
    if len(kids) < 2:
        return []
    f = _omml("f")
    f.append(_omml_wrap("num", _walk_mathml(kids[0])))
    f.append(_omml_wrap("den", _walk_mathml(kids[1])))
    return [f]


def _handle_msup(node) -> List[OxmlElement]:
    kids = list(node)
    if len(kids) < 2:
        return []
    # latex2mathml в inline-режиме оборачивает \sum^{n}, \int^{n} и т.п.
    # в msup/msub/msubsup — детектируем N-ary базу до обычного sSup.
    nary_chr = _is_nary_op(kids[0])
    if nary_chr:
        return [_build_nary(nary_chr, [], _walk_mathml(kids[1]),
                            hide_sub=True)]
    s = _omml("sSup")
    s.append(_omml_wrap("e", _walk_mathml(kids[0])))
    s.append(_omml_wrap("sup", _walk_mathml(kids[1])))
    return [s]


def _handle_msub(node) -> List[OxmlElement]:
    kids = list(node)
    if len(kids) < 2:
        return []
    nary_chr = _is_nary_op(kids[0])
    if nary_chr:
        return [_build_nary(nary_chr, _walk_mathml(kids[1]), [],
                            hide_sup=True)]
    s = _omml("sSub")
    s.append(_omml_wrap("e", _walk_mathml(kids[0])))
    s.append(_omml_wrap("sub", _walk_mathml(kids[1])))
    return [s]


def _handle_msubsup(node) -> List[OxmlElement]:
    kids = list(node)
    if len(kids) < 3:
        return []
    nary_chr = _is_nary_op(kids[0])
    if nary_chr:
        return [_build_nary(nary_chr,
                            _walk_mathml(kids[1]),
                            _walk_mathml(kids[2]))]
    s = _omml("sSubSup")
    s.append(_omml_wrap("e", _walk_mathml(kids[0])))
    s.append(_omml_wrap("sub", _walk_mathml(kids[1])))
    s.append(_omml_wrap("sup", _walk_mathml(kids[2])))
    return [s]


def _handle_msqrt(node) -> List[OxmlElement]:
    rad = _omml("rad")
    radPr = _omml("radPr")
    degHide = _omml("degHide")
    _set_mval(degHide, "1")
    radPr.append(degHide)
    rad.append(radPr)
    rad.append(_omml("deg"))
    e_kids: List[OxmlElement] = []
    for child in node:
        e_kids.extend(_walk_mathml(child))
    rad.append(_omml_wrap("e", e_kids))
    return [rad]


def _handle_mroot(node) -> List[OxmlElement]:
    kids = list(node)
    if len(kids) < 2:
        return []
    rad = _omml("rad")
    rad.append(_omml_wrap("deg", _walk_mathml(kids[1])))
    rad.append(_omml_wrap("e", _walk_mathml(kids[0])))
    return [rad]


def _handle_mover(node) -> List[OxmlElement]:
    # Семантика MathML: kids[0] = база, kids[1] = надстрочный (overscript).
    # Это либо акцент (\bar, \hat, \vec) либо предел над оператором.
    kids = list(node)
    if len(kids) < 2:
        return []
    base, over = kids[0], kids[1]

    # Акцент: либо явный accent="true", либо overscript это <mo> с
    # одним маркером из _ACCENT_CHARS.
    over_text = (over.text or "").strip() if _ml_local(over.tag) == "mo" else ""
    is_accent = (
        node.get("accent") == "true"
        or (len(over_text) <= 2 and over_text in _ACCENT_CHARS)
    )
    if is_accent and over_text:
        # Spacing-знак (¯ ^ ~ ¨ ´ ` ˘ ˙ ˇ →) перевести в combining-форму,
        # иначе Word рисует его на baseline и накладывает поверх буквы.
        accent_chr = _ACCENT_NORMALIZE.get(over_text, over_text)
        acc = _omml("acc")
        accPr = _omml("accPr")
        chr_el = _omml("chr")
        _set_mval(chr_el, accent_chr)
        accPr.append(chr_el)
        acc.append(accPr)
        acc.append(_omml_wrap("e", _walk_mathml(base)))
        return [acc]

    # N-ary оператор с одним только верхним пределом (редко, но возможно).
    nary_chr = _is_nary_op(base)
    if nary_chr:
        return [_build_nary(nary_chr, [], _walk_mathml(over),
                            hide_sub=True)]

    # Иначе: оператор с верхним пределом → m:limUpp.
    lu = _omml("limUpp")
    lu.append(_omml_wrap("e", _walk_mathml(base)))
    lu.append(_omml_wrap("lim", _walk_mathml(over)))
    return [lu]


def _handle_munder(node) -> List[OxmlElement]:
    # kids[0] = база, kids[1] = подстрочный (underscript).
    kids = list(node)
    if len(kids) < 2:
        return []
    base, under = kids[0], kids[1]

    nary_chr = _is_nary_op(base)
    if nary_chr:
        return [_build_nary(nary_chr, _walk_mathml(under), [],
                            hide_sup=True)]

    ll = _omml("limLow")
    ll.append(_omml_wrap("e", _walk_mathml(base)))
    ll.append(_omml_wrap("lim", _walk_mathml(under)))
    return [ll]


def _handle_munderover(node) -> List[OxmlElement]:
    # kids[0] = база, kids[1] = under, kids[2] = over.
    kids = list(node)
    if len(kids) < 3:
        return []
    base, under, over = kids[0], kids[1], kids[2]

    nary_chr = _is_nary_op(base)
    if nary_chr:
        return [_build_nary(nary_chr,
                            _walk_mathml(under),
                            _walk_mathml(over))]

    # Generic fallback: вложенные limUpp(limLow(...)).
    ll = _omml("limLow")
    ll.append(_omml_wrap("e", _walk_mathml(base)))
    ll.append(_omml_wrap("lim", _walk_mathml(under)))
    lu = _omml("limUpp")
    lu.append(_omml_wrap("e", [ll]))
    lu.append(_omml_wrap("lim", _walk_mathml(over)))
    return [lu]


def _handle_mfenced(node) -> List[OxmlElement]:
    open_chr = node.get("open", "(")
    close_chr = node.get("close", ")")
    d = _omml("d")
    dPr = _omml("dPr")
    if open_chr != "(":
        beg = _omml("begChr")
        _set_mval(beg, open_chr)
        dPr.append(beg)
    if close_chr != ")":
        end = _omml("endChr")
        _set_mval(end, close_chr)
        dPr.append(end)
    if list(dPr):
        d.append(dPr)
    e_kids = []
    for child in node:
        e_kids.extend(_walk_mathml(child))
    d.append(_omml_wrap("e", e_kids))
    return [d]


def _handle_mtable(node) -> List[OxmlElement]:
    return [_build_mtable(node, left_align=False)]


# Тег MathML → handler. Adding a new construct = one entry + one
# top-level function (no surgery inside _walk_mathml).
_MATHML_HANDLERS = {
    "math": _handle_container,
    "mstyle": _handle_container,
    "mrow": _handle_container,
    "semantics": _handle_container,
    "annotation": _handle_container,
    "mi": _handle_mi,
    "mn": _handle_simple_text,
    "mo": _handle_simple_text,
    "mtext": _handle_simple_text,
    "mspace": _handle_mspace,
    "mfrac": _handle_mfrac,
    "msup": _handle_msup,
    "msub": _handle_msub,
    "msubsup": _handle_msubsup,
    "msqrt": _handle_msqrt,
    "mroot": _handle_mroot,
    "mover": _handle_mover,
    "munder": _handle_munder,
    "munderover": _handle_munderover,
    "mfenced": _handle_mfenced,
    "mtable": _handle_mtable,
}


def _walk_mathml(node) -> List[OxmlElement]:
    """Рекурсивный обход MathML-узла, возвращает список OMML-элементов.

    Возвращаем именно список (а не один узел), потому что mrow/mstyle
    плющатся в плоский список детей при подстановке в обёртки типа
    <m:e>, <m:num>, <m:sup>.
    """
    handler = _MATHML_HANDLERS.get(_ml_local(node.tag))
    if handler is not None:
        return handler(node)
    # Неизвестный тег — рекурсивно обрабатываем детей, не падаем.
    result: List[OxmlElement] = []
    for child in node:
        result.extend(_walk_mathml(child))
    return result


def _latex_to_omath(latex: str) -> OxmlElement:
    """LaTeX-строка → <m:oMath> готовый к вставке в параграф."""
    try:
        import latex2mathml.converter as _l2m
    except ImportError as e:
        raise ImportError(
            "r.formula() requires the 'latex2mathml' package. "
            "If you launched the script through scripts/ensure_env.py, the "
            "venv setup must have failed. Otherwise install manually: "
            "pip install latex2mathml"
        ) from e

    mml_str = _l2m.convert(latex)
    tree = _ET.fromstring(mml_str)
    children = _walk_mathml(tree)
    omath = _omml("oMath")
    for c in children:
        omath.append(c)
    return omath


# Публичный алиас (исторически функция называлась _latex_to_omath).
latex_to_omath = _latex_to_omath
