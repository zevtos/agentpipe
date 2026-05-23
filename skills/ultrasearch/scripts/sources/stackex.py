"""
sources/stackex.py — Stack Exchange API 2.3 search/advanced.

No-key default: 300 req/day. With free `STACKEX_API_KEY`: 10 000 req/day
(register a tiny app at stackapps.com/apps/oauth/register; no OAuth needed
for read-only key — just pass it as the `key` query param).

We hit /search/advanced for one site per call (parallel across the
profile's `sites` config list). Backoff field is honored if returned.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

from _common import get_env, log
from ._base import Candidate

_BASE = "https://api.stackexchange.com/2.3"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
_DEFAULT_SITES: tuple[str, ...] = ("stackoverflow",)


def _params(query: str, site: str, page_size: int) -> dict[str, Any]:
    p: dict[str, Any] = {
        "order": "desc",
        "sort": "votes",
        "q": query,
        "site": site,
        "pagesize": page_size,
        "filter": "withbody",   # include question body
    }
    key = get_env("STACKEX_API_KEY") or get_env("STACK_EXCHANGE_KEY")
    if key:
        p["key"] = key
    return p


async def _query_one_site(
    client: httpx.AsyncClient, query: str, site: str, page_size: int
) -> list[Candidate]:
    try:
        resp = await client.get(
            f"{_BASE}/search/advanced",
            params=_params(query, site, page_size),
        )
    except httpx.HTTPError as e:
        log(f"stackex {site}: HTTP error {type(e).__name__}")
        return []

    if resp.status_code != 200:
        log(f"stackex {site}: HTTP {resp.status_code}")
        return []

    try:
        data = resp.json()
    except ValueError:
        log(f"stackex {site}: response not JSON")
        return []

    backoff = data.get("backoff")
    if backoff:
        log(f"stackex {site}: backoff {backoff}s requested")
        await asyncio.sleep(min(int(backoff), 30))

    items = data.get("items") or []
    out: list[Candidate] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        qid = it.get("question_id")
        if not qid:
            continue

        creation_ts = it.get("creation_date")
        year: int | None = None
        if creation_ts:
            try:
                year = int(time.gmtime(int(creation_ts)).tm_year)
            except (TypeError, ValueError):
                pass

        body = (it.get("body") or "")
        # crude HTML strip — SE body is HTML; one-pass tag removal is fine
        # for an abstract preview (full body never lands here)
        abstract = _HTML_TAG_RE.sub(" ", body)
        abstract = _WS_RE.sub(" ", abstract).strip()[:2000] or None

        owner = it.get("owner") or {}
        author = owner.get("display_name") or "anonymous"
        url = it.get("link") or f"https://{site}.com/q/{qid}"

        is_answered = bool(it.get("is_answered"))
        score = int(it.get("score") or 0)
        accepted = bool(it.get("accepted_answer_id"))

        extras = {
            "site": site,
            "score": score,
            "view_count": int(it.get("view_count") or 0),
            "answer_count": int(it.get("answer_count") or 0),
            "is_answered": is_answered,
            "accepted_answer": accepted,
            "tags": it.get("tags") or [],
            "owner_reputation": int((owner or {}).get("reputation") or 0),
            "creation_date": creation_ts,
        }
        c = Candidate(
            source_id="stackex",
            source_type="qa",
            title=it.get("title") or "(no title)",
            url=url,
            external_id=f"so:{site}:{qid}",
            abstract=abstract,
            authors=[author],
            year=year,
            score_raw=extras,
            extras=extras,
        )
        out.append(c)
    log(f"stackex {site}: {len(out)} questions")
    return out


async def fetch(
    query: str,
    *,
    config: dict[str, Any] | None = None,
    max_items: int = 30,
    **_: Any,
) -> list[Candidate]:
    config = dict(config or {})
    sites = config.get("sites") or _DEFAULT_SITES
    if isinstance(sites, str):
        sites = (sites,)
    per_site = max(1, min(100, max_items // max(1, len(sites))))

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        tasks = [_query_one_site(client, query, site, per_site) for site in sites]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    out: list[Candidate] = []
    for r in results:
        if isinstance(r, Exception):
            log(f"stackex: task error {r!r}")
            continue
        out.extend(r)
    return out[:max_items]


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "FastAPI vs Flask"
    res = asyncio.run(fetch(q, config={"sites": ["stackoverflow"]}, max_items=5))
    for c in res:
        print(f"  {c.external_id} ↑{c.extras['score']} — {c.title}")
