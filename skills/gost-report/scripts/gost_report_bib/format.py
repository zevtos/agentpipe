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


# ===========================================================================
# ГОСТ Р 7.0.100–2018 (краткое описание): профиль bib_style="7.0.100-2018".
# ===========================================================================
#
#   Алексеев П. В. Философия : учебник / П. В. Алексеев, А. В. Панин. – 4-е изд.,
#   перераб. и доп. – Москва : Проспект, 2010. – 592 с. – ISBN 978-5-392-01042-4. –
#   Текст : непосредственный.
#
# Отличия от 7.1-2003: инициалы через пробел («П. В.»), город полностью
# («Москва»), нет «[Электронный ресурс]» — в конце область вида содержания
# «Текст : непосредственный» / «Текст : электронный», у статьи она стоит перед
# «//». 1–3 автора — первый в заголовке; 4 — запись под заглавием, все после
# « / »; 5 и больше — первые три и «[и др.]».

_CITY_FULL = {"М.": "Москва", "СПб.": "Санкт-Петербург", "Л.": "Ленинград",
              "Н. Новгород": "Нижний Новгород", "Ростов н/Д": "Ростов-на-Дону"}
_INITIAL_SPLIT = re.compile(r"([A-ZА-ЯЁ][a-zа-яё]?\.)\s*")
_RANGE = re.compile(r"(?<=\d)\s*[-—–]\s*(?=\d)")


def _spaced_initials(initials: str) -> str:
    """«П.В.» → «П. В.»."""
    return " ".join(_INITIAL_SPLIT.findall(initials)) or initials.strip()


def _person(name: str) -> Optional[Tuple[str, str]]:
    sp = _split_name(name)
    if sp:
        return sp[0], _spaced_initials(sp[1])
    return None


def _resp_name(name: str) -> str:
    sp = _person(name)
    return f"{sp[1]} {sp[0]}" if sp else name.strip()


def _resp_list(authors: List[str]) -> str:
    if len(authors) >= 5:
        return ", ".join(_resp_name(a) for a in authors[:3]) + " [и др.]"
    return ", ".join(_resp_name(a) for a in authors)


def _as_list(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def _range(text: str) -> str:
    return _RANGE.sub("–", text)


def _join_areas(segments: List[str], sep: str) -> str:
    segs = []
    for s in segments:
        s = (s or "").strip()
        if not s:
            continue
        if s.endswith(".") and not s.endswith(("и др.]", "...")):
            s = s[:-1].rstrip()
        segs.append(s)
    return sep.join(segs)


def _title_area_100(entry: dict, *, with_heading: bool = True) -> str:
    authors = _author_list(entry.get("authors"))
    title = (entry.get("title") or "").strip()
    heading = (entry.get("heading") or "").strip()
    if not heading and with_heading and 1 <= len(authors) <= 3 and _person(authors[0]):
        fam, ini = _person(authors[0])
        heading = f"{fam} {ini}"
    s = f"{heading} {title}" if heading else title
    for sub in _as_list(entry.get("subtitle")):
        s += f" : {sub}"
    resp = []
    # Диссертация: в сведениях об ответственности автор полностью — его
    # передают в responsibility, инициалы из authors тогда не повторяются.
    thesis_full = (entry.get("type") == "thesis" and entry.get("responsibility"))
    if authors and entry.get("resp_authors", True) and not thesis_full:
        resp.append(_resp_list(authors))
    resp.extend(_as_list(entry.get("responsibility")))
    if resp:
        s += " / " + " ; ".join(resp)
    return s


def _publication_100(entry: dict) -> str:
    places = entry.get("places")
    year = str(entry.get("year") or "").strip()
    if places:
        loc = " ; ".join(f"{_CITY_FULL.get(c.strip(), c.strip())} : {p.strip()}"
                         for c, p in places)
    else:
        city = (entry.get("city") or "").strip()
        city = _CITY_FULL.get(city, city)
        pub = (entry.get("publisher") or "").strip()
        loc = f"{city} : {pub}" if city and pub else city or pub
    if loc and year:
        return f"{loc}, {year}"
    return loc or year


def _extent_100(entry: dict, foreign: bool) -> str:
    pages = str(entry.get("pages") or "").strip()
    if not pages:
        return ""
    if re.fullmatch(r"\d+", pages):
        return f"{pages} {'p.' if foreign else 'с.'}"
    return _range(pages)


def _url_100(entry: dict) -> List[str]:
    out = []
    url = (entry.get("url") or "").strip()
    if url:
        accessed = (entry.get("accessed") or "").strip()
        out.append(f"URL: {url}" + (f" (дата обращения: {accessed})" if accessed else ""))
    mode = (entry.get("access_mode") or "").strip()
    if mode:
        out.append(f"Режим доступа: {mode}")
    return out


def _content_100(entry: dict) -> str:
    if entry.get("content"):
        return str(entry["content"]).strip()
    electronic = bool(entry.get("url")) or (entry.get("type") or "") in ("web", "electronic", "online")
    return "Текст : электронный" if electronic else "Текст : непосредственный"


def _host_100(entry: dict, sep: str, foreign: bool) -> str:
    """Часть после « // »: журнал, сборник, газета, сайт."""
    host = (entry.get("journal") or entry.get("proceedings") or entry.get("host")
            or entry.get("site") or "").strip()
    head = host
    for sub in _as_list(entry.get("host_subtitle")):
        head += f" : {sub}"
    resp = _as_list(entry.get("host_responsibility") or entry.get("editors"))
    if resp:
        head += " / " + " ; ".join(resp)
    vol = str(entry.get("volume") or "").strip()
    iss = str(entry.get("issue") or "").strip()
    numbering = ", ".join(x for x in ((f"Т. {vol}" if vol else ""),
                                      (f"№ {iss}" if iss else "")) if x)
    pages = str(entry.get("pages") or "").strip()
    pagespart = f"{'P.' if foreign else 'С.'} {_range(pages)}" if pages else ""
    if entry.get("city") or entry.get("publisher") or entry.get("places"):
        middle = [_publication_100(entry)]
    else:
        middle = [str(entry.get("year") or "").strip()]
    date = (entry.get("date") or "").strip()
    return _join_areas([head, *middle, date, numbering, pagespart, *_url_100(entry)], sep)


def format_entry_7_0_100(entry: dict, dash: str = "–") -> str:
    """dict → запись по ГОСТ Р 7.0.100–2018. type: book | article | conference |
    web | standard | law | thesis | disk. Сверх полей 7.1-2003: isbn, series,
    notes (список примечаний: «Библиогр.: с. 37–44»), access_mode («для
    авторизир. пользователей»), content (свой вид содержания), heading (свой
    заголовок: «Российская Федерация. Законы.»), places ([(город, изд-во), …]),
    date («10 марта» для газеты), host / site (издание для «//»),
    host_subtitle, editors (сведения об ответственности издания).
    subtitle и responsibility могут быть списками."""
    sep = f". {dash} "
    typ = (entry.get("type") or "book").strip().lower()
    foreign = _is_foreign(entry)
    authors = _author_list(entry.get("authors"))
    analytic = typ in ("article", "conference") or (
        typ in ("web", "law") and (entry.get("journal") or entry.get("site")
                                   or entry.get("host")))
    if typ == "standard" and not entry.get("heading") and entry.get("number"):
        entry = {**entry, "heading": str(entry["number"]).strip() + "."}
    title_area = _title_area_100(entry, with_heading=len(authors) <= 3)
    if analytic:
        body = _join_areas([title_area, _content_100(entry)], sep)
        body += " // " + _host_100(entry, sep, foreign)
        doi = (entry.get("doi") or "").strip()
        if doi:
            body = _join_areas([body, f"DOI {doi}"], sep)
        return body.rstrip(".") + "."
    series = (entry.get("series") or "").strip()
    isbn = (entry.get("isbn") or "").strip()
    doi = (entry.get("doi") or "").strip()
    areas = [
        title_area,
        (entry.get("edition") or "").strip(),
        _publication_100(entry),
        _extent_100(entry, foreign),
        f"({series})" if series else "",
        *_as_list(entry.get("notes")),
        f"ISBN {isbn}" if isbn else "",
        f"DOI {doi}" if doi else "",
        *_url_100(entry),
        _content_100(entry),
    ]
    return _join_areas(areas, sep).rstrip(".") + "."


def format_entry_styled(entry: dict, style: str = "7.1-2003", dash: str = "–") -> str:
    """Запись по стилю профиля (bib_style)."""
    if style == "7.0.100-2018":
        return format_entry_7_0_100(entry, dash=dash)
    return format_entry(entry)
