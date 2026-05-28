"""
sources/hn.py — Hacker News search via Algolia (hn.algolia.com/api/v2).

No auth, no rate limit (publicly generous). We hit /search to get stories
ranked by points, filtered to type=story, with `hitsPerPage=max_items`.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from _common import default_http_timeout, log
from ._base import Candidate

_BASE = "https://hn.algolia.com/api/v1"
_TIMEOUT = default_http_timeout(read=20.0)


async def fetch(
    query: str,
    *,
    config: dict[str, Any] | None = None,
    max_items: int = 30,
    **_: Any,
) -> list[Candidate]:
    config = dict(config or {})
    hits_per_page = max(1, min(100, int(max_items)))
    params = {
        "query": query,
        "tags": "story",
        "hitsPerPage": hits_per_page,
        "restrictSearchableAttributes": "title,url,story_text",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{_BASE}/search", params=params)
    except httpx.HTTPError as e:
        log(f"hn: HTTP error {type(e).__name__}")
        return []

    if resp.status_code != 200:
        log(f"hn: HTTP {resp.status_code}")
        return []

    try:
        data = resp.json()
    except ValueError:
        log("hn: response not JSON")
        return []

    out: list[Candidate] = []
    for h in data.get("hits") or []:
        if not isinstance(h, dict):
            continue
        oid = h.get("objectID")
        if not oid:
            continue
        title = h.get("title") or h.get("story_title") or "(no title)"
        url = h.get("url") or f"https://news.ycombinator.com/item?id={oid}"
        author = h.get("author") or "anonymous"

        created_at = h.get("created_at_i")
        year: int | None = None
        if created_at:
            try:
                year = time.gmtime(int(created_at)).tm_year
            except (TypeError, ValueError):
                pass

        points = int(h.get("points") or 0)
        num_comments = int(h.get("num_comments") or 0)
        extras = {
            "points": points,
            "num_comments": num_comments,
            "created_at_i": created_at,
            "author": author,
            "story_text": (h.get("story_text") or "")[:500],
        }
        c = Candidate(
            source_id="hn",
            source_type="forum",
            title=title,
            url=url,
            external_id=f"hn:story:{oid}",
            abstract=(h.get("story_text") or "")[:2000] or None,
            authors=[author],
            year=year,
            score_raw=extras,
            extras=extras,
        )
        out.append(c)
    log(f"hn: {len(out)} stories for q={query!r}")
    return out


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "FastAPI"
    res = asyncio.run(fetch(q, max_items=5))
    for c in res:
        print(f"  {c.external_id} ↑{c.extras['points']} — {c.title}")
