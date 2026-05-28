"""
sources/pypi.py — PyPI JSON API for Python-package metadata + download trends.

Two endpoints used:
  https://pypi.org/pypi/{name}/json          -- metadata
  https://pypistats.org/api/packages/{name}/recent  -- download trend

Like deps_dev, the search step is best-effort: we extract candidate package
names from the query and probe each in parallel.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from _common import default_http_timeout, log
from ._base import Candidate, tokenize_package_names as _tokenize

_PYPI = "https://pypi.org/pypi"
_PYPISTATS = "https://pypistats.org/api/packages"
_TIMEOUT = default_http_timeout(read=20.0)


async def _probe(client: httpx.AsyncClient, name: str) -> Candidate | None:
    try:
        resp = await client.get(f"{_PYPI}/{name}/json")
    except httpx.HTTPError as e:
        log(f"pypi {name}: HTTP error {type(e).__name__}")
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None

    info = data.get("info") or {}
    pkg_name = info.get("name") or name
    version = info.get("version") or ""
    summary = info.get("summary") or ""
    home = info.get("home_page") or info.get("project_url") or ""
    license_ = info.get("license") or ""
    author = info.get("author") or info.get("maintainer") or ""

    # downloads — try pypistats (one extra round trip, best-effort)
    downloads_last_month: int | None = None
    try:
        dresp = await client.get(f"{_PYPISTATS}/{pkg_name.lower()}/recent")
        if dresp.status_code == 200:
            d = dresp.json().get("data") or {}
            downloads_last_month = int(d.get("last_month") or 0)
    except (httpx.HTTPError, ValueError):
        pass

    extras = {
        "version": version,
        "summary": summary,
        "license": license_,
        "home_page": home,
        "downloads_last_month": downloads_last_month,
        "author": author,
    }
    return Candidate(
        source_id="pypi",
        source_type="pkg",
        title=f"{pkg_name} {version}".strip(),
        url=f"https://pypi.org/project/{pkg_name}/",
        external_id=f"pypi:{pkg_name.lower()}",
        abstract=summary[:2000] or None,
        authors=[author] if author else [],
        score_raw=extras,
        extras=extras,
    )


async def fetch(
    query: str,
    *,
    config: dict[str, Any] | None = None,
    max_items: int = 30,
    **_: Any,
) -> list[Candidate]:
    config = dict(config or {})
    names = _tokenize(query)
    if not names:
        return []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        tasks = [_probe(client, name) for name in names]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[Candidate] = []
    for r in results:
        if isinstance(r, Exception):
            log(f"pypi: task error {r!r}")
            continue
        if r is not None:
            out.append(r)
    log(f"pypi: {len(out)} package matches")
    return out[:max_items]


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "fastapi litestar"
    res = asyncio.run(fetch(q, max_items=8))
    for c in res:
        print(f"  {c.external_id} dl={c.extras.get('downloads_last_month')} — {c.title}")
