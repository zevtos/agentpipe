"""Форматирование библиографической записи по ГОСТ Р 7.0.5-2008.

Распространённая «студенческая» форма (вузы варьируют детали — поля настраиваемы).
Разделитель « — » обязателен по стандарту, в выводе сохраняется как есть.
"""
from __future__ import annotations

from typing import List, Union

DASH = " — "       # внутриобластной em-dash (хвост DOI и т.п.)
AREA = ". — "      # ГОСТ-разделитель ОБЛАСТЕЙ описания: точка, пробел, тире, пробел


def join_authors(authors: Union[str, List[str], None]) -> str:
    """1-3 автора — все через запятую; 4+ — первый + « [и др.]» (ГОСТ Р 7.0.5)."""
    if not authors:
        return ""
    if isinstance(authors, str):
        return authors.strip()
    a = [x.strip() for x in authors if x and x.strip()]
    if not a:
        return ""
    if len(a) >= 4:
        return a[0] + " [и др.]"
    return ", ".join(a)


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


def _head(entry: dict) -> str:
    authors = join_authors(entry.get("authors"))
    title = (entry.get("title") or "").strip()
    head = (authors + " " + title).strip() if authors else title
    edition = (entry.get("edition") or "").strip()
    if edition:
        head = f"{head}. {edition}"
    return head


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


def _with_doi(body: str, entry: dict) -> str:
    doi = (entry.get("doi") or "").strip()
    if doi:
        body = body.rstrip(".") + f"{AREA}DOI: {doi}."
    return body


def format_entry(entry: dict) -> str:
    """dict с полями → строка библиоописания ГОСТ Р 7.0.5. type:
    book | article | web | conference | standard | thesis (по умолчанию book)."""
    typ = (entry.get("type") or "book").strip().lower()
    head = _head(entry)
    year = str(entry.get("year") or "").strip()
    pages = str(entry.get("pages") or "").strip()

    if typ == "article":
        journal = (entry.get("journal") or "").strip()
        first = f"{head} // {journal}" if journal else head
        pagespart = f"С. {pages}" if pages else ""
        body = _dash_join([first, year, _vol_issue(entry), pagespart])
        return _with_doi(body, entry)

    if typ in ("web", "electronic", "online"):
        url = (entry.get("url") or "").strip()
        accessed = (entry.get("accessed") or "").strip()
        first = f"{head} [Электронный ресурс]"
        urlpart = ""
        if url:
            urlpart = f"URL: {url}"
            if accessed:
                urlpart += f" (дата обращения: {accessed})"
        body = _dash_join([first, year, urlpart])
        return _with_doi(body, entry)

    if typ == "conference":
        # Доклад в сборнике трудов конференции.
        proc = (entry.get("journal") or entry.get("proceedings") or "").strip()
        first = f"{head} // {proc}" if proc else head
        pagespart = f"С. {pages}" if pages else ""
        body = _dash_join([first, _imprint(entry), pagespart])
        return _with_doi(body, entry)

    # book / standard / thesis / fallback
    pagespart = f"{pages} с." if pages else ""
    body = _dash_join([head, _imprint(entry), pagespart])
    return _with_doi(body, entry)
