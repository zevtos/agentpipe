"""Форматирование библиографической записи по ГОСТ 7.1-2003 / ГОСТ Р 7.0.5-2008.

Структура записи (ГОСТ 7.1-2003, 7.80-2000, 7.82-2001):

    Заголовок Заглавие [Электронный ресурс] : сведения к заглавию / сведения
    об ответственности ; иные сведения. — Издание. — Место : Издательство,
    Год. — Объём. — URL: … (дата обращения: …).

1-3 автора: в заголовке первый («Фамилия И.О.»), после « / » все в форме
«И.О. Фамилия». 4+ автора: запись начинается с заглавия, после « / » первый
с «[и др.]». Коллективный автор (не «Фамилия И.О.») в заголовок не выносится.
Разделитель « — » обязателен по стандарту, в выводе сохраняется как есть.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple, Union

DASH = " — "       # внутриобластной em-dash (хвост DOI и т.п.)
AREA = ". — "      # ГОСТ-разделитель ОБЛАСТЕЙ описания: точка, пробел, тире, пробел

_INITIALS = re.compile(r"^(?:[A-ZА-ЯЁ][a-zа-яё]?\.\s?){1,3}$")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def _split_name(name: str) -> Optional[Tuple[str, str]]:
    """«Немет Э.» → («Немет», «Э.»); не персона («Oracle Corporation») → None."""
    parts = name.strip().split(None, 1)
    if len(parts) == 2 and _INITIALS.match(parts[1].strip()):
        return parts[0], parts[1].strip()
    return None


def _invert(name: str) -> str:
    """«Фамилия И.О.» → «И.О. Фамилия» (форма сведений об ответственности)."""
    sp = _split_name(name)
    return f"{sp[1]} {sp[0]}" if sp else name.strip()


def _author_list(authors: Union[str, List[str], None]) -> List[str]:
    if not authors:
        return []
    if isinstance(authors, str):
        parts = [x.strip() for x in authors.split(",") if x.strip()]
        if parts and all(_split_name(x) for x in parts):
            return parts
        return [authors.strip()]
    return [x.strip() for x in authors if x and x.strip()]


def join_authors(authors: Union[str, List[str], None]) -> str:
    """Сведения об ответственности: 1-3 автора все, 4+ первый + « [и др.]»."""
    a = _author_list(authors)
    if not a:
        return ""
    if len(a) >= 4:
        return _invert(a[0]) + " [и др.]"
    return ", ".join(_invert(x) for x in a)


def _is_foreign(entry: dict) -> bool:
    return not _CYRILLIC.search(entry.get("title") or "")


def _dash_join(segments: List[str]) -> str:
    """Склейка областей описания через ГОСТ-разделитель «. — » (точка завершает
    предыдущую область, дальше тире). С каждой области снимается лишняя концевая
    точка, в конце ставится одна."""
    segs = []
    for s in segments:
        s = (s or "").strip()
        if not s:
            continue
        if s.endswith("."):
            s = s[:-1].rstrip()
        segs.append(s)
    body = AREA.join(segs)
    if body and not body.endswith("."):
        body += "."
    return body


def _title_area(entry: dict, electronic: bool) -> str:
    """Заголовок + область заглавия и сведений об ответственности."""
    authors = _author_list(entry.get("authors"))
    title = (entry.get("title") or "").strip()
    heading = authors[0] if 1 <= len(authors) <= 3 and _split_name(authors[0]) else ""
    s = f"{heading} {title}" if heading else title
    if electronic:
        s += " [Электронный ресурс]"
    subtitle = (entry.get("subtitle") or "").strip()
    if subtitle:
        s += f" : {subtitle}"
    resp = [x for x in (join_authors(authors),
                        (entry.get("responsibility") or "").strip()) if x]
    if resp:
        s += " / " + " ; ".join(resp)
    return s


def _imprint(entry: dict) -> str:
    city = (entry.get("city") or "").strip()
    pub = (entry.get("publisher") or "").strip()
    year = str(entry.get("year") or "").strip()
    if city and pub:
        loc = f"{city} : {pub}"
    else:
        loc = city or pub
    if loc and year:
        return f"{loc}, {year}"
    return loc or year


def _vol_issue(entry: dict) -> str:
    vol = str(entry.get("volume") or "").strip()
    iss = str(entry.get("issue") or "").strip()
    if vol and iss:
        return f"Т. {vol}, № {iss}"
    if iss:
        return f"№ {iss}"
    if vol:
        return f"Т. {vol}"
    return ""


def _url(entry: dict) -> str:
    url = (entry.get("url") or "").strip()
    if not url:
        return ""
    accessed = (entry.get("accessed") or "").strip()
    return f"URL: {url}" + (f" (дата обращения: {accessed})" if accessed else "")


def _with_doi(body: str, entry: dict) -> str:
    doi = (entry.get("doi") or "").strip()
    if doi:
        body = body.rstrip(".") + f"{AREA}DOI: {doi}."
    return body


def format_entry(entry: dict) -> str:
    """dict с полями → строка библиоописания ГОСТ 7.1-2003. type:
    book | article | web | conference | standard | thesis (по умолчанию book).
    Необязательные: subtitle (сведения к заглавию), responsibility (иные
    сведения: «пер. с англ.»), edition, url/accessed (→ [Электронный ресурс])."""
    typ = (entry.get("type") or "book").strip().lower()
    electronic = typ in ("web", "electronic", "online") or bool(entry.get("url"))
    head = _title_area(entry, electronic)
    edition = (entry.get("edition") or "").strip()
    year = str(entry.get("year") or "").strip()
    pages = str(entry.get("pages") or "").strip()
    foreign = _is_foreign(entry)

    if typ in ("article", "conference"):
        host = (entry.get("journal") or entry.get("proceedings") or "").strip()
        first = f"{head} // {host}" if host else head
        pagespart = f"{'P.' if foreign else 'С.'} {pages}" if pages else ""
        if typ == "article":
            body = _dash_join([first, year, _vol_issue(entry), pagespart, _url(entry)])
        else:
            body = _dash_join([first, _imprint(entry), pagespart, _url(entry)])
        return _with_doi(body, entry)

    # book / web / standard / thesis / fallback
    pagespart = f"{pages} {'p.' if foreign else 'с.'}" if pages else ""
    body = _dash_join([head, edition, _imprint(entry), pagespart, _url(entry)])
    return _with_doi(body, entry)
