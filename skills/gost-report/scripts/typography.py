"""Русская типографика для gost-report: неразрывные пробелы, тире, кавычки, выделение.

Чистые функции без зависимостей, включаются через Report(typography=True).
Правила и источники: references/typography.md.
"""
from __future__ import annotations

import re
from typing import List, Tuple

NBSP = " "
THIN = " "  # узкий неразрывный: «л. с.», «т. е.», инициалы

# Предлоги, союзы и частицы, которые нельзя оставлять в конце строки.
SHORT_WORDS = frozenset(
    "а в и к о с у во да до за из ко на не ни но об от по со "
    "без для как над обо или под при про что".split()
)
_SHORT_RE = re.compile(
    r"(?<![\w-])(" + "|".join(sorted(SHORT_WORDS, key=len, reverse=True)) + r")[ \t]+(?=\S)",
    re.IGNORECASE,
)
_PARTICLE_RE = re.compile(r"[ \t]+(?=(?:же|ли|бы)(?![\w-]))")
_NOUN_NUMBER_RE = re.compile(
    r"(?<![\w-])((?:рисун|табл|глав|раздел|пункт|страниц|формул|приложени)[а-яё]*)[ \t]+(?=\d)",
    re.IGNORECASE,
)
_LATIN_NUMBER_RE = re.compile(r"(?<![\w-])([A-Z][A-Za-z]*)[ \t]+(?=[A-Z]*\d)")
# Только самостоятельное число («2015 году», «400–430 л.»), не «B58 выполнен» и не «АИ-98 мотор».
# Предлог после числа не клеим: цепочка «от 326 до 387 л. с.» не влезла бы в строку.
_NUMBER_UNIT_RE = re.compile(
    r"(?<![\w-])(\d[\d.,–]*)[ \t]+"
    r"(?!(?:" + "|".join(sorted(SHORT_WORDS, key=len, reverse=True)) + r")(?![\w-]))"
    r"(?=[А-Яа-яЁё%°]|[A-Za-z]{1,3}(?![\w-]))"
)
_ABBR_RE = re.compile(r"(?<![\w-])(т|л|н)\.[ \t]*(е|д|п|к|н|о|с|э)\.")
_REF_ABBR_RE = re.compile(r"(?<![\w-])(см|рис|табл|стр|гл|ул|им)\.[ \t]+")
_INITIALS_AFTER_RE = re.compile(r"([А-ЯЁ][а-яё]+)[ \t]+([А-ЯЁ]\.)[ \t]*([А-ЯЁ]\.)")
_INITIALS_BEFORE_RE = re.compile(r"(?<![\w-])([А-ЯЁ]\.)[ \t]*([А-ЯЁ]\.)[ \t]+(?=[А-ЯЁ][а-яё])")
_RANGE_RE = re.compile(r"(?<![\w/.,-])(\d+(?:[.,]\d+)?)-(\d+(?:[.,]\d+)?)(?![\w/-])")
_EMPHASIS_RE = re.compile(r"(?<!\\)(\*\*|\*)")


def _quotes(text: str) -> str:
    """Прямые кавычки → «ёлочки», вложенные → „лапки“."""
    if '"' not in text:
        return text
    out, depth = [], 0
    for i, ch in enumerate(text):
        if ch != '"':
            out.append(ch)
            continue
        prev = text[i - 1] if i else " "
        opening = prev.isspace() or prev in "([{«„—–-"
        if opening:
            out.append("«" if depth == 0 else "„")
            depth += 1
        else:
            depth = max(depth - 1, 0)
            out.append("»" if depth == 0 else "“")
    return "".join(out)


def typograf(text: str, *, dashes: bool = False) -> str:
    """Расставляет неразрывные пробелы и правит знаки. Идемпотентна.

    dashes=True дополнительно меняет дефис-тире на настоящие знаки: « - » → « — »,
    «400-430» → «400–430». Без него тире в тексте не появляются (правило №1 скилла).
    Перевод строки не трогает: им разбивают подзаголовок по смыслу.
    """
    if not text:
        return text
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = text.replace("...", "…")
    text = _quotes(text)
    if dashes:
        text = re.sub(r"(?<=\S)[ \t]+[-–][ \t]+(?=\S)", " — ", text)
        text = _RANGE_RE.sub("\\1–\\2", text)
    text = re.sub(r"[ \t]+—", NBSP + "—", text)
    text = re.sub(r"^—[ \t]+", "—" + NBSP, text, flags=re.MULTILINE)
    text = _ABBR_RE.sub(lambda m: f"{m[1]}.{THIN}{m[2]}.", text)
    text = _REF_ABBR_RE.sub(lambda m: f"{m[1]}.{NBSP}", text)
    text = _INITIALS_AFTER_RE.sub(lambda m: f"{m[1]}{NBSP}{m[2]}{THIN}{m[3]}", text)
    text = _INITIALS_BEFORE_RE.sub(lambda m: f"{m[1]}{THIN}{m[2]}{NBSP}", text)
    text = re.sub(r"([№§])[ \t]*(?=\d)", "\\1" + NBSP, text)
    text = _NOUN_NUMBER_RE.sub(lambda m: m[1] + NBSP, text)
    text = _LATIN_NUMBER_RE.sub(lambda m: m[1] + NBSP, text)
    text = _NUMBER_UNIT_RE.sub(lambda m: m[1] + NBSP, text)
    text = _SHORT_RE.sub(lambda m: m[1] + NBSP, text)
    text = _PARTICLE_RE.sub(NBSP, text)
    return text


def split_emphasis(text: str) -> List[Tuple[str, bool, bool]]:
    """Разбирает **полужирный** и *курсив* на куски (текст, bold, italic).

    Звёздочка с пробелами вокруг («2 * 3») остаётся знаком, \\* экранирует.
    Незакрытое выделение — ValueError: иначе жирным ушёл бы весь хвост абзаца.
    """
    parts: List[Tuple[str, bool, bool]] = []
    bold = italic = False
    pos = 0

    def flush(end: int) -> None:
        chunk = text[pos:end].replace("\\*", "*")
        if chunk:
            parts.append((chunk, bold, italic))

    for m in _EMPHASIS_RE.finditer(text):
        mark = m.group(1)
        before = text[m.start() - 1] if m.start() else " "
        after = text[m.end()] if m.end() < len(text) else " "
        active = bold if mark == "**" else italic
        if (active and before.isspace()) or (not active and after.isspace()):
            continue
        flush(m.start())
        if mark == "**":
            bold = not bold
        else:
            italic = not italic
        pos = m.end()
    if bold or italic:
        raise ValueError(
            f"Незакрытое выделение ** или * (оно не должно пересекать $...$): «{text[:60]}»"
        )
    flush(len(text))
    return parts
