"""
sources/deps_dev.py — Google's deps.dev API for dependency graph & security.

No auth, generous rate limits. Endpoint shape:
  https://api.deps.dev/v3/systems/{system}/packages/{name}
where system ∈ {pypi, npm, cargo, maven, go, nuget}.

For ultrasearch we use deps.dev as a *signal* source: given a free-text
query, we treat space-separated tokens as candidate package names, probe
PyPI and npm in parallel, and surface whichever ones resolve. This is a
deliberate Stage 1 simplification — proper search will use the BigQuery
mirror in Stage 2.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from _common import default_http_timeout, log
from ._base import Candidate, tokenize_package_names as _tokenize

_BASE = "https://api.deps.dev/v3"
_TIMEOUT = default_http_timeout(read=20.0)


async def _probe(
    client: httpx.AsyncClient, system: str, name: str
) -> Candidate | None:
    try:
        resp = await client.get(f"{_BASE}/systems/{system}/packages/{name}")
    except httpx.HTTPError as e:
        log(f"deps_dev {system}/{name}: HTTP error {type(e).__name__}")
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None

    versions = data.get("versions") or []
    if not versions:
        return None
    latest = versions[-1] if isinstance(versions, list) else {}
    if not isinstance(latest, dict):
        latest = {}
    version_key = latest.get("versionKey") or {}
    version = version_key.get("version") or ""

    pkg_url = (
        f"https://pypi.org/project/{name}/" if system == "pypi"
        else f"https://www.npmjs.com/package/{name}" if system == "npm"
        else f"https://deps.dev/{system}/{name}"
    )
    extras = {
        "system": system,
        "package_name": name,
        "latest_version": version,
        "published_at": latest.get("publishedAt"),
        "is_default": bool(latest.get("isDefault")),
    }
    return Candidate(
        source_id="deps_dev",
        source_type="pkg",
        title=f"{system}:{name}",
        url=pkg_url,
        external_id=f"deps:{system}:{name}",
        abstract=f"{system} package {name} (latest {version or 'unknown'})",
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
    systems = config.get("systems") or ("pypi", "npm")
    if isinstance(systems, str):
        systems = (systems,)

    candidates = _tokenize(query)
    if not candidates:
        return []

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        tasks = [
            _probe(client, sys, name)
            for sys in systems
            for name in candidates
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    out: list[Candidate] = []
    for r in results:
        if isinstance(r, Exception):
            log(f"deps_dev: task error {r!r}")
            continue
        if r is not None:
            out.append(r)
    log(f"deps_dev: {len(out)} package matches across {systems}")
    return out[:max_items]


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "fastapi flask litestar"
    res = asyncio.run(fetch(q, max_items=10))
    for c in res:
        print(f"  {c.external_id} v{c.extras.get('latest_version')} — {c.title}")
