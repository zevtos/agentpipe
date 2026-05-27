#!/usr/bin/env python3
"""
discover.py — async fan-out to OpenAlex + Semantic Scholar + arXiv.

Stage 1 scope (research §13 line 268; 01_stage1.md §3.1):
    - Three sources, three concurrent tasks under asyncio.gather.
    - Per-domain rate budgets via aiometer (ADR-005).
    - Dedup by normalize_doi(doi) or 'T:' + casefold_title(title).
    - Merge prefers OpenAlex > S2 > arXiv for source label; preserves
      referenced_works (only OpenAlex populates them — needed by Stage 2
      citation traversal).

Stage 2 will add: Crossref (habanero), Europe PMC, CORE, paperscraper.
Stage 3 will add: КиберЛенинка OAI-PMH, OATD, BASE, DataCite, HF Hub.

CLI:
    discover.py --query "<topic>" [--max-per-source N] [--json] [--sources a,b,c]

Env vars (read at startup, fail fast if mandatory ones missing):
    OPENALEX_API_KEY  (mandatory post 13 Feb 2026 — free key from openalex.org)
    OPENALEX_EMAIL    (recommended, polite-pool identifier)
    S2_API_KEY        (optional — dedicated 1 RPS without it goes to shared pool)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Iterable, Literal, TypedDict

import httpx

from _common import (
    casefold_title,
    emit_failure,
    get_env,
    iso_now,
    json_stdout,
    log,
    nfc,
    normalize_doi,
    require_env,
    short_id,
)


# -------- constants --------

# Per-domain rate budgets — research §2 table, ADR-005.
RATE_BUDGET: dict[str, float] = {
    "openalex":  10.0,   # research §2 line 26: 100 RPS hard; we stay polite
    "s2":         1.0,   # research §2 line 27: dedicated key = 1 RPS
    "arxiv":      0.33,  # research §2 line 28: 3s between requests
    "crossref":   5.0,   # research §2 line 29: polite ≤50 RPS, stay polite
    "europepmc":  5.0,   # research §2 line 30: conservative
    "core":       0.17,  # research §2 line 32: ~10 req/min free tier
    "unpaywall":  5.0,   # research §2 line 31: soft 100k/day cap
}

USER_AGENT = "ultrasearch/0.1 (https://github.com/zevtos/agentpipe; mailto:{email})"
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
PER_SOURCE_TIMEOUT_S = 60.0
DEFAULT_MAX_PER_SOURCE = 50
MAX_RETRIES = 5

OPENALEX_BASE = "https://api.openalex.org"
S2_BASE = "https://api.semanticscholar.org/graph/v1"
ARXIV_BASE = "http://export.arxiv.org/api/query"
CROSSREF_BASE = "https://api.crossref.org/works"
EUROPEPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
CORE_BASE = "https://api.core.ac.uk/v3/search/works"
# Stage 3 sources
KIBERLENINKA_BASE = "https://cyberleninka.ru/article"   # search via HTML scraping
OATD_BASE = "https://oatd.org/oatd/search"
BASE_BASE = "https://api.base-search.net/cgi-bin/BaseHttpSearchInterface.fcgi"
DATACITE_BASE = "https://api.datacite.org/dois"
HF_BASE = "https://huggingface.co/api/papers"


class Candidate(TypedDict, total=False):
    source: Literal["openalex", "s2", "arxiv", "crossref", "europepmc", "core",
                    "kiberleninka", "oatd", "base", "datacite", "hf"]
    doi: str | None
    arxiv_id: str | None
    openalex_id: str | None
    s2_paper_id: str | None
    title: str
    title_norm: str
    abstract: str | None
    year: int | None
    authors: list[str]
    venue: str | None
    cited_by_count: int | None
    referenced_works: list[str]
    pdf_url_hint: str | None
    raw_id: str


# -------- helpers --------

def _candidate_key(c: Candidate) -> str:
    d = normalize_doi(c.get("doi"))
    if d:
        return d
    title_norm = c.get("title_norm") or casefold_title(c.get("title") or "")
    return "T:" + title_norm


def _source_rank(s: str) -> int:
    return {
        "openalex": 0, "s2": 1, "crossref": 2,
        "europepmc": 3, "core": 4, "arxiv": 5,
        "datacite": 6, "base": 7, "oatd": 8, "hf": 9, "kiberleninka": 10,
    }.get(s, 99)


def _merge(a: Candidate, b: Candidate) -> Candidate:
    """Merge two candidates that share a dedup key. Preference order:
    preserve any field that's set on the higher-rank source; for `authors`
    prefer the longer list; for `cited_by_count` take the max; for
    `referenced_works` always prefer OpenAlex (the only source that has
    them in Stage 1)."""
    if _source_rank(a.get("source", "")) > _source_rank(b.get("source", "")):
        a, b = b, a  # `a` is now the higher-priority source
    merged: Candidate = dict(a)  # type: ignore[assignment]
    for k, v in b.items():
        if k == "authors":
            if not merged.get("authors") and v:
                merged["authors"] = v
            elif v and len(v) > len(merged.get("authors") or []):
                merged["authors"] = v
        elif k == "cited_by_count":
            cur = merged.get("cited_by_count")
            if v is not None and (cur is None or v > cur):
                merged["cited_by_count"] = v
        elif k == "referenced_works":
            if v and not merged.get("referenced_works"):
                merged["referenced_works"] = v
        elif merged.get(k) in (None, "", []):
            merged[k] = v  # type: ignore[literal-required]
    return merged


def dedup(cands: Iterable[Candidate]) -> list[Candidate]:
    seen: dict[str, Candidate] = {}
    for c in cands:
        if not c.get("title"):
            continue
        c["title_norm"] = casefold_title(c["title"])
        k = _candidate_key(c)
        if k in seen:
            seen[k] = _merge(seen[k], c)
        else:
            seen[k] = c
    return list(seen.values())


async def _retry_get(client: httpx.AsyncClient, url: str, *, params: dict | None = None,
                     headers: dict | None = None, source: str) -> httpx.Response | None:
    """GET with exponential backoff on 429/5xx. Returns the final Response or
    None if all retries exhausted."""
    delay = 1.0
    last: httpx.Response | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = await client.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
        except httpx.HTTPError as e:
            log(f"{source}: http error {type(e).__name__} attempt {attempt}/{MAX_RETRIES}: {e}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 32.0)
            continue
        last = r
        if r.status_code == 429 or 500 <= r.status_code < 600:
            log(f"{source}: HTTP {r.status_code} attempt {attempt}/{MAX_RETRIES}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 32.0)
            continue
        return r
    return last


# -------- OpenAlex --------

def _abstract_from_inverted_index(inv: dict[str, list[int]] | None) -> str | None:
    """OpenAlex returns abstracts as an inverted index {word: [positions]}.
    Reconstruct into a flat string."""
    if not inv:
        return None
    flat: list[tuple[int, str]] = []
    for word, positions in inv.items():
        for p in positions:
            flat.append((p, word))
    flat.sort()
    return " ".join(w for _, w in flat) or None


def _openalex_to_candidate(w: dict[str, Any]) -> Candidate | None:
    title = w.get("title") or w.get("display_name")
    if not title:
        return None
    doi = normalize_doi(w.get("doi"))
    ids = w.get("ids") or {}
    openalex_id = w.get("id") or ids.get("openalex")
    if openalex_id and openalex_id.startswith("https://"):
        openalex_id = openalex_id.rsplit("/", 1)[-1]
    authors: list[str] = []
    for a in (w.get("authorships") or []):
        name = (a.get("author") or {}).get("display_name")
        if name:
            authors.append(name)
    venue = None
    primary_loc = (w.get("primary_location") or {})
    src = (primary_loc.get("source") or {})
    if src.get("display_name"):
        venue = src["display_name"]
    pdf_url = primary_loc.get("pdf_url")
    if not pdf_url:
        boa = (w.get("best_oa_location") or {})
        pdf_url = boa.get("pdf_url")
    arxiv_id = None
    # OpenAlex sometimes stores arXiv id in ids.openalex_id or as a doi 10.48550/arXiv.XXXX
    if doi and doi.startswith("10.48550/arxiv."):
        arxiv_id = doi.split("10.48550/arxiv.")[-1]
    referenced = w.get("referenced_works") or []
    # strip URL prefix
    referenced = [r.rsplit("/", 1)[-1] if r.startswith("https://") else r for r in referenced]
    return {
        "source": "openalex",
        "doi": doi,
        "arxiv_id": arxiv_id,
        "openalex_id": openalex_id,
        "s2_paper_id": None,
        "title": nfc(title),
        "title_norm": casefold_title(title),
        "abstract": _abstract_from_inverted_index(w.get("abstract_inverted_index")),
        "year": w.get("publication_year"),
        "authors": authors,
        "venue": venue,
        "cited_by_count": w.get("cited_by_count"),
        "referenced_works": referenced,
        "pdf_url_hint": pdf_url,
        "raw_id": str(w.get("id") or ""),
    }


async def _query_openalex(client: httpx.AsyncClient, query: str, n: int,
                          api_key: str | None, email: str | None) -> list[Candidate]:
    params = {
        "search": query,
        "per-page": min(n, 100),
        "select": "id,doi,title,display_name,publication_year,authorships,"
                  "primary_location,best_oa_location,cited_by_count,"
                  "referenced_works,abstract_inverted_index,ids",
    }
    if api_key:
        params["api_key"] = api_key
    if email:
        params["mailto"] = email
    headers = {"User-Agent": USER_AGENT.format(email=email or "anonymous")}
    r = await _retry_get(client, f"{OPENALEX_BASE}/works", params=params,
                         headers=headers, source="openalex")
    if r is None or r.status_code != 200:
        sc = "no_response" if r is None else r.status_code
        log(f"openalex: giving up after retries ({sc})")
        return []
    try:
        data = r.json()
    except Exception as e:
        log(f"openalex: bad JSON: {e}")
        return []
    results = data.get("results") or []
    out: list[Candidate] = []
    for w in results[:n]:
        c = _openalex_to_candidate(w)
        if c:
            out.append(c)
    return out


# -------- Semantic Scholar --------

def _s2_to_candidate(p: dict[str, Any]) -> Candidate | None:
    title = p.get("title")
    if not title:
        return None
    ext = p.get("externalIds") or {}
    doi = normalize_doi(ext.get("DOI"))
    arxiv_id = ext.get("ArXiv")
    authors = []
    for a in (p.get("authors") or []):
        name = a.get("name")
        if name:
            authors.append(name)
    venue = p.get("venue") or None
    return {
        "source": "s2",
        "doi": doi,
        "arxiv_id": arxiv_id,
        "openalex_id": None,
        "s2_paper_id": p.get("paperId"),
        "title": nfc(title),
        "title_norm": casefold_title(title),
        "abstract": p.get("abstract"),
        "year": p.get("year"),
        "authors": authors,
        "venue": venue,
        "cited_by_count": p.get("citationCount"),
        "referenced_works": [],
        "pdf_url_hint": ((p.get("openAccessPdf") or {}).get("url")),
        "raw_id": p.get("paperId") or "",
    }


async def _query_s2(client: httpx.AsyncClient, query: str, n: int,
                    api_key: str | None) -> list[Candidate]:
    # /paper/search is closed to keyless clients (always 429 since ~2024). /paper/search/bulk
    # still works without a key but only supports paperId/publicationDate/citationCount sorts —
    # no relevance ranking. citationCount:desc is the best ranking proxy for lit review.
    # No `limit` on bulk; it returns up to 1000 per call, we slice to n.
    params: dict[str, Any] = {
        "query": query,
        "fields": "title,abstract,year,authors,venue,citationCount,externalIds,"
                  "openAccessPdf,paperId",
        "sort": "citationCount:desc",
    }
    headers = {"User-Agent": USER_AGENT.format(email="anonymous")}
    if api_key:
        headers["x-api-key"] = api_key
    r = await _retry_get(client, f"{S2_BASE}/paper/search/bulk", params=params,
                         headers=headers, source="s2")
    if r is None or r.status_code != 200:
        sc = "no_response" if r is None else r.status_code
        log(f"s2: giving up after retries ({sc})")
        return []
    try:
        data = r.json()
    except Exception as e:
        log(f"s2: bad JSON: {e}")
        return []
    results = data.get("data") or []
    out: list[Candidate] = []
    for p in results[:n]:
        c = _s2_to_candidate(p)
        if c:
            out.append(c)
    return out


# -------- Crossref --------

def _crossref_to_candidate(item: dict[str, Any]) -> Candidate | None:
    title_list = item.get("title") or []
    title = title_list[0] if title_list else None
    if not title:
        return None
    doi = normalize_doi(item.get("DOI"))
    authors = []
    for a in (item.get("author") or []):
        name = " ".join(filter(None, [a.get("given"), a.get("family")])).strip()
        if name:
            authors.append(name)
        elif a.get("name"):
            authors.append(a["name"])
    year = None
    issued = item.get("issued") or {}
    date_parts = (issued.get("date-parts") or [[]])[0]
    if date_parts:
        year = date_parts[0]
    venue = None
    container = item.get("container-title") or []
    if container:
        venue = container[0]
    return {
        "source": "crossref",
        "doi": doi,
        "arxiv_id": None,
        "openalex_id": None,
        "s2_paper_id": None,
        "title": nfc(title),
        "title_norm": casefold_title(title),
        "abstract": (item.get("abstract") or "").strip() or None,
        "year": year,
        "authors": authors,
        "venue": venue,
        "cited_by_count": item.get("is-referenced-by-count"),
        "referenced_works": [],
        "pdf_url_hint": None,
        "raw_id": item.get("DOI") or "",
    }


async def _query_crossref(client: httpx.AsyncClient, query: str, n: int,
                          email: str | None) -> list[Candidate]:
    params: dict[str, str | int] = {
        "query": query,
        "rows": min(n, 100),
        "select": "DOI,title,author,issued,container-title,abstract,"
                  "is-referenced-by-count,type",
    }
    if email:
        params["mailto"] = email
    headers = {"User-Agent": USER_AGENT.format(email=email or "anonymous")}
    r = await _retry_get(client, CROSSREF_BASE, params=params, headers=headers,
                         source="crossref")
    if r is None or r.status_code != 200:
        log(f"crossref: giving up ({'no_response' if r is None else r.status_code})")
        return []
    try:
        data = r.json()
    except Exception as e:
        log(f"crossref: bad JSON: {e}")
        return []
    items = (data.get("message") or {}).get("items") or []
    out: list[Candidate] = []
    for item in items[:n]:
        c = _crossref_to_candidate(item)
        if c:
            out.append(c)
    return out


# -------- Europe PMC --------

def _europepmc_to_candidate(item: dict[str, Any]) -> Candidate | None:
    title = (item.get("title") or "").strip().rstrip(".")
    if not title:
        return None
    doi = normalize_doi(item.get("doi"))
    authors: list[str] = []
    author_list = item.get("authorList") or {}
    for a in (author_list.get("author") or []):
        name = a.get("fullName") or a.get("collectiveName")
        if name:
            authors.append(name)
    if not authors and item.get("authorString"):
        authors = [s.strip() for s in item["authorString"].split(",") if s.strip()]
    year = None
    pub_year = item.get("pubYear")
    if pub_year:
        try:
            year = int(pub_year)
        except (TypeError, ValueError):
            year = None
    pdf_url = None
    for f in (item.get("fullTextUrlList") or {}).get("fullTextUrl") or []:
        if f.get("documentStyle", "").lower() == "pdf":
            pdf_url = f.get("url")
            break
    return {
        "source": "europepmc",
        "doi": doi,
        "arxiv_id": None,
        "openalex_id": None,
        "s2_paper_id": None,
        "title": nfc(title),
        "title_norm": casefold_title(title),
        "abstract": (item.get("abstractText") or "").strip() or None,
        "year": year,
        "authors": authors,
        "venue": item.get("journalTitle"),
        "cited_by_count": item.get("citedByCount"),
        "referenced_works": [],
        "pdf_url_hint": pdf_url,
        "raw_id": (item.get("id") or item.get("pmid") or item.get("pmcid") or ""),
    }


async def _query_europepmc(client: httpx.AsyncClient, query: str, n: int
                           ) -> list[Candidate]:
    params: dict[str, str | int] = {
        "query": query,
        "format": "json",
        "resultType": "core",
        "pageSize": min(n, 100),
    }
    r = await _retry_get(client, EUROPEPMC_BASE, params=params, source="europepmc")
    if r is None or r.status_code != 200:
        log(f"europepmc: giving up ({'no_response' if r is None else r.status_code})")
        return []
    try:
        data = r.json()
    except Exception as e:
        log(f"europepmc: bad JSON: {e}")
        return []
    items = (data.get("resultList") or {}).get("result") or []
    out: list[Candidate] = []
    for item in items[:n]:
        c = _europepmc_to_candidate(item)
        if c:
            out.append(c)
    return out


# -------- CORE --------

def _core_to_candidate(item: dict[str, Any]) -> Candidate | None:
    title = (item.get("title") or "").strip()
    if not title:
        return None
    doi = normalize_doi(item.get("doi"))
    authors: list[str] = []
    for a in (item.get("authors") or []):
        if isinstance(a, dict):
            name = a.get("name")
            if name:
                authors.append(name)
        elif isinstance(a, str):
            authors.append(a)
    year = item.get("yearPublished") or item.get("publishedDate")
    if isinstance(year, str) and len(year) >= 4:
        try:
            year = int(year[:4])
        except ValueError:
            year = None
    pdf_url = item.get("downloadUrl")
    venue = None
    journals = item.get("journals") or []
    if journals:
        venue = journals[0].get("title") if isinstance(journals[0], dict) else None
    return {
        "source": "core",
        "doi": doi,
        "arxiv_id": None,
        "openalex_id": None,
        "s2_paper_id": None,
        "title": nfc(title),
        "title_norm": casefold_title(title),
        "abstract": (item.get("abstract") or "").strip() or None,
        "year": year if isinstance(year, int) else None,
        "authors": authors,
        "venue": venue,
        "cited_by_count": None,
        "referenced_works": [],
        "pdf_url_hint": pdf_url,
        "raw_id": str(item.get("id") or ""),
    }


async def _query_core(client: httpx.AsyncClient, query: str, n: int,
                      api_key: str | None) -> list[Candidate]:
    if not api_key:
        log("core: CORE_API_KEY unset, skipping")
        return []
    params: dict[str, str | int] = {
        "q": query,
        "limit": min(n, 100),
    }
    headers = {
        "User-Agent": USER_AGENT.format(email="anonymous"),
        "Authorization": f"Bearer {api_key}",
    }
    r = await _retry_get(client, CORE_BASE, params=params, headers=headers,
                         source="core")
    if r is None or r.status_code != 200:
        log(f"core: giving up ({'no_response' if r is None else r.status_code})")
        return []
    try:
        data = r.json()
    except Exception as e:
        log(f"core: bad JSON: {e}")
        return []
    results = data.get("results") or []
    out: list[Candidate] = []
    for item in results[:n]:
        c = _core_to_candidate(item)
        if c:
            out.append(c)
    return out


# -------- DataCite --------

def _datacite_to_candidate(item: dict[str, Any]) -> Candidate | None:
    attrs = item.get("attributes") or {}
    titles = attrs.get("titles") or []
    title = (titles[0].get("title") if titles else None) or attrs.get("title")
    if not title:
        return None
    doi = normalize_doi(attrs.get("doi"))
    creators = attrs.get("creators") or []
    authors = [c.get("name") for c in creators if c.get("name")]
    year = attrs.get("publicationYear")
    if year and not isinstance(year, int):
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = None
    venue = attrs.get("publisher")
    return {
        "source": "datacite",
        "doi": doi,
        "arxiv_id": None,
        "openalex_id": None,
        "s2_paper_id": None,
        "title": nfc(title),
        "title_norm": casefold_title(title),
        "abstract": None,
        "year": year,
        "authors": authors,
        "venue": venue,
        "cited_by_count": None,
        "referenced_works": [],
        "pdf_url_hint": None,
        "raw_id": item.get("id") or "",
    }


async def _query_datacite(client: httpx.AsyncClient, query: str, n: int
                          ) -> list[Candidate]:
    params = {"query": query, "page[size]": min(n, 100)}
    r = await _retry_get(client, DATACITE_BASE, params=params, source="datacite")
    if r is None or r.status_code != 200:
        return []
    try:
        data = r.json()
    except Exception:
        return []
    out: list[Candidate] = []
    for item in (data.get("data") or [])[:n]:
        c = _datacite_to_candidate(item)
        if c:
            out.append(c)
    return out


# -------- HuggingFace Hub (daily papers) --------

async def _query_hf(client: httpx.AsyncClient, query: str, n: int
                    ) -> list[Candidate]:
    """HF /api/papers returns the daily-papers feed; we filter by query string
    keyword match. Not a full-text search but useful for the ML domain."""
    try:
        r = await client.get(HF_BASE, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError:
        return []
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    query_lc = query.lower()
    out: list[Candidate] = []
    for item in data:
        paper = item.get("paper") or {}
        title = (paper.get("title") or "").strip()
        summary = (paper.get("summary") or "").strip()
        if not (title and (query_lc in title.lower() or query_lc in summary.lower())):
            continue
        arxiv_id = paper.get("id")  # HF uses arXiv id
        out.append({
            "source": "hf",
            "doi": None,
            "arxiv_id": arxiv_id,
            "openalex_id": None,
            "s2_paper_id": None,
            "title": nfc(title),
            "title_norm": casefold_title(title),
            "abstract": summary or None,
            "year": None,
            "authors": [a.get("name") for a in (paper.get("authors") or [])
                        if a.get("name")],
            "venue": "HuggingFace Papers",
            "cited_by_count": None,
            "referenced_works": [],
            "pdf_url_hint": (f"https://arxiv.org/pdf/{arxiv_id}.pdf"
                             if arxiv_id else None),
            "raw_id": arxiv_id or "",
        })
        if len(out) >= n:
            break
    return out


# -------- OATD (Open Access Theses & Dissertations) --------
# Stage 3: deferred to a stub. oatd.org has no JSON API; HTML scraping required.
# We register the source so users can opt in via --sources but emit empty for now.

async def _query_oatd(query: str, n: int) -> list[Candidate]:
    # TODO Stage 3.1: implement OATD HTML parsing (trafilatura + selector list).
    return []


# -------- BASE (Bielefeld Academic Search Engine) --------
# Stage 3: stub — BASE requires OAI-PMH or a free API key; scaffold reserved.

async def _query_base(query: str, n: int) -> list[Candidate]:
    return []


# -------- КиберЛенинка (RU) --------
# Stage 3: stub — cyberleninka has an undocumented OAI-PMH endpoint (research
# §2 line 47); reliable scraping requires careful selector list. The orchestrator
# pre-translates RU queries via translate.py before calling Western sources,
# so this is opportunistic coverage for native RU literature.

async def _query_kiberleninka(query: str, n: int) -> list[Candidate]:
    return []


# -------- arXiv --------

def _arxiv_id_from_url(s: str | None) -> str | None:
    if not s:
        return None
    # e.g. "http://arxiv.org/abs/2409.13740v2" → "2409.13740"
    last = s.rstrip("/").rsplit("/", 1)[-1]
    last = last.split("v")[0] if "v" in last else last
    return last or None


async def _query_arxiv(query: str, n: int) -> list[Candidate]:
    """Use the `arxiv` package — its sync iterator is blocking; we run it in
    a thread so the asyncio loop stays unblocked."""
    def _do() -> list[Candidate]:
        try:
            import arxiv  # type: ignore
        except Exception as e:
            log(f"arxiv: not importable: {e}")
            return []
        # If query looks like a bare arxiv id, use id_list for direct lookup.
        bare_id = None
        q = query.strip()
        if q.replace(".", "").replace("v", "").isdigit() and "." in q:
            bare_id = q.split("v")[0]
        client = arxiv.Client(page_size=min(n, 50), delay_seconds=3.0, num_retries=3)
        if bare_id:
            search = arxiv.Search(id_list=[bare_id], max_results=n)
        else:
            search = arxiv.Search(query=q, max_results=n,
                                  sort_by=arxiv.SortCriterion.Relevance)
        out: list[Candidate] = []
        try:
            for r in client.results(search):
                aid = _arxiv_id_from_url(getattr(r, "entry_id", None))
                title = nfc(r.title or "")
                doi = normalize_doi(getattr(r, "doi", None))
                year = None
                if r.published:
                    year = r.published.year
                authors = [a.name for a in (r.authors or []) if a.name]
                out.append({
                    "source": "arxiv",
                    "doi": doi,
                    "arxiv_id": aid,
                    "openalex_id": None,
                    "s2_paper_id": None,
                    "title": title,
                    "title_norm": casefold_title(title),
                    "abstract": (r.summary or "").strip() or None,
                    "year": year,
                    "authors": authors,
                    "venue": "arXiv",
                    "cited_by_count": None,
                    "referenced_works": [],
                    "pdf_url_hint": getattr(r, "pdf_url", None),
                    "raw_id": aid or "",
                })
        except Exception as e:
            log(f"arxiv: query failed: {e}")
        return out
    return await asyncio.to_thread(_do)


# -------- public API --------

DEFAULT_SOURCES = ("openalex", "s2", "arxiv", "crossref", "europepmc", "core")
STAGE3_SOURCES = (
    "openalex", "s2", "arxiv", "crossref", "europepmc", "core",
    "datacite", "hf", "kiberleninka", "oatd", "base",
)


async def discover(query: str, *,
                   max_per_source: int = DEFAULT_MAX_PER_SOURCE,
                   sources: tuple[str, ...] = DEFAULT_SOURCES,
                   openalex_api_key: str | None = None,
                   openalex_email: str | None = None,
                   s2_api_key: str | None = None,
                   core_api_key: str | None = None) -> dict[str, Any]:
    """Fan out to each source concurrently. Returns a dict with merged
    candidates and per-source counts. Caller dedups via `dedup()`."""
    openalex_api_key = openalex_api_key or get_env("OPENALEX_API_KEY")
    openalex_email = openalex_email or get_env("OPENALEX_EMAIL")
    s2_api_key = s2_api_key or get_env("S2_API_KEY")
    core_api_key = core_api_key or get_env("CORE_API_KEY")

    per_source: dict[str, int] = {}
    all_cands: list[Candidate] = []
    warnings: list[str] = []

    headers = {"User-Agent": USER_AGENT.format(email=openalex_email or "anonymous")}
    async with httpx.AsyncClient(headers=headers, http2=True) as client:
        tasks = []
        names = []
        if "openalex" in sources:
            tasks.append(asyncio.wait_for(
                _query_openalex(client, query, max_per_source,
                                openalex_api_key, openalex_email),
                timeout=PER_SOURCE_TIMEOUT_S,
            ))
            names.append("openalex")
        if "s2" in sources:
            tasks.append(asyncio.wait_for(
                _query_s2(client, query, max_per_source, s2_api_key),
                timeout=PER_SOURCE_TIMEOUT_S,
            ))
            names.append("s2")
        if "arxiv" in sources:
            tasks.append(asyncio.wait_for(
                _query_arxiv(query, max_per_source),
                timeout=PER_SOURCE_TIMEOUT_S,
            ))
            names.append("arxiv")
        if "crossref" in sources:
            tasks.append(asyncio.wait_for(
                _query_crossref(client, query, max_per_source, openalex_email),
                timeout=PER_SOURCE_TIMEOUT_S,
            ))
            names.append("crossref")
        if "europepmc" in sources:
            tasks.append(asyncio.wait_for(
                _query_europepmc(client, query, max_per_source),
                timeout=PER_SOURCE_TIMEOUT_S,
            ))
            names.append("europepmc")
        if "core" in sources:
            tasks.append(asyncio.wait_for(
                _query_core(client, query, max_per_source, core_api_key),
                timeout=PER_SOURCE_TIMEOUT_S,
            ))
            names.append("core")
        if "datacite" in sources:
            tasks.append(asyncio.wait_for(
                _query_datacite(client, query, max_per_source),
                timeout=PER_SOURCE_TIMEOUT_S,
            ))
            names.append("datacite")
        if "hf" in sources:
            tasks.append(asyncio.wait_for(
                _query_hf(client, query, max_per_source),
                timeout=PER_SOURCE_TIMEOUT_S,
            ))
            names.append("hf")
        if "oatd" in sources:
            tasks.append(asyncio.wait_for(
                _query_oatd(query, max_per_source),
                timeout=PER_SOURCE_TIMEOUT_S,
            ))
            names.append("oatd")
        if "base" in sources:
            tasks.append(asyncio.wait_for(
                _query_base(query, max_per_source),
                timeout=PER_SOURCE_TIMEOUT_S,
            ))
            names.append("base")
        if "kiberleninka" in sources:
            tasks.append(asyncio.wait_for(
                _query_kiberleninka(query, max_per_source),
                timeout=PER_SOURCE_TIMEOUT_S,
            ))
            names.append("kiberleninka")
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, res in zip(names, results):
            if isinstance(res, BaseException):
                warnings.append(f"{name}: {type(res).__name__}: {res}")
                per_source[name] = 0
                continue
            per_source[name] = len(res)
            all_cands.extend(res)

    unique = dedup(all_cands)
    return {
        "ok": True,
        "query": query,
        "n_candidates": len(all_cands),
        "n_unique": len(unique),
        "per_source": per_source,
        "warnings": warnings,
        "candidates": unique,
        "timestamp": iso_now(),
    }


# -------- CLI --------

def main() -> int:
    parser = argparse.ArgumentParser(prog="discover.py",
                                     description="Discover candidate papers across OpenAlex+S2+arXiv.")
    parser.add_argument("--query", required=True, help="research topic")
    parser.add_argument("--max-per-source", type=int, default=DEFAULT_MAX_PER_SOURCE)
    parser.add_argument("--sources", default=",".join(DEFAULT_SOURCES),
                        help="comma-separated subset of "
                             "{openalex,s2,arxiv,crossref,europepmc,core}")
    parser.add_argument("--json", action="store_true",
                        help="emit one-line JSON on stdout (else pretty-printed)")
    args = parser.parse_args()

    # Stage 1: OPENALEX_API_KEY is recommended but not yet hard-required.
    # After 13 Feb 2026 OpenAlex requires it; we still let the run proceed
    # with a warning so the smoke flow works on a brand-new install.
    if not get_env("OPENALEX_API_KEY"):
        log("OPENALEX_API_KEY not set — running in legacy mode; get a free key at https://openalex.org/account")

    sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    try:
        result = asyncio.run(discover(
            args.query,
            max_per_source=args.max_per_source,
            sources=sources,
        ))
    except KeyboardInterrupt:
        emit_failure("interrupted")
        return 130

    if args.json:
        json_stdout(result)
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
