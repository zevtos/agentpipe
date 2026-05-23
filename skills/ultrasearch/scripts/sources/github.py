"""
sources/github.py — GitHub repo + code search via REST API.

Endpoint: https://api.github.com/search/repositories?q=...
Auth: optional `GH_TOKEN` env var → 5000 req/h vs 60 req/h unauth.

Returns Candidates with extras={stars, forks, archived, language,
pushed_at, license, open_issues}. score_dev() in scoring/dev.py consumes
these signals.

Stage 1 keeps the implementation to repo search only — code search and
issue search will land in 1.5 when the profile expands.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from _common import get_env, log
from ._base import Candidate, SourceError

_BASE = "https://api.github.com"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
_USER_AGENT = "ultrasearch/0.15 (+https://github.com/zevtos/agentpipe)"


def _headers() -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": _USER_AGENT,
    }
    token = get_env("GH_TOKEN") or get_env("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # GitHub returns Z-suffixed UTC timestamps.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _build_q(query: str, config: dict[str, Any]) -> str:
    """GitHub search qualifiers per https://docs.github.com/en/search-github."""
    parts = [query]
    stars_min = config.get("stars_min")
    if stars_min:
        parts.append(f"stars:>={int(stars_min)}")
    max_days = config.get("last_commit_max_days")
    if max_days:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(max_days))).date().isoformat()
        parts.append(f"pushed:>={cutoff}")
    lang = config.get("language")
    if lang:
        parts.append(f"language:{lang}")
    return " ".join(parts)


async def fetch(
    query: str,
    *,
    config: dict[str, Any] | None = None,
    max_items: int = 30,
    **_: Any,
) -> list[Candidate]:
    """Search GitHub repos. Returns up to `max_items` Candidates.

    On 401/403 (rate limit or invalid token) we log + return [] rather than
    raising — orchestrate.py treats empty results as a soft failure.
    """
    config = dict(config or {})
    q = _build_q(query, config)
    params = {
        "q": q,
        "sort": "stars",
        "order": "desc",
        "per_page": min(max(1, int(max_items)), 100),
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_headers()) as client:
            resp = await client.get(f"{_BASE}/search/repositories", params=params)
    except httpx.HTTPError as e:
        log(f"github: HTTP error {type(e).__name__}; returning empty")
        return []

    if resp.status_code == 403:
        # Most often rate limit — log and degrade.
        log(f"github: 403 (rate limit or auth); returning empty. "
            f"Set GH_TOKEN for 5000 req/h.")
        return []
    if resp.status_code == 401:
        log("github: 401 (bad token); returning empty")
        return []
    if resp.status_code >= 400:
        log(f"github: HTTP {resp.status_code}; returning empty")
        return []

    try:
        data = resp.json()
    except ValueError:
        log("github: response was not JSON")
        return []

    items = data.get("items") or []
    out: list[Candidate] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        full_name = it.get("full_name") or ""
        if not full_name:
            continue

        pushed_at_str = it.get("pushed_at")
        pushed_at = _parse_iso(pushed_at_str)
        last_commit_days: int | None = None
        if pushed_at is not None:
            delta = datetime.now(timezone.utc) - pushed_at
            last_commit_days = max(0, delta.days)

        extras = {
            "stars": int(it.get("stargazers_count") or 0),
            "forks": int(it.get("forks_count") or 0),
            "archived": bool(it.get("archived")),
            "language": it.get("language"),
            "pushed_at": pushed_at_str,
            "license": (it.get("license") or {}).get("spdx_id"),
            "open_issues": int(it.get("open_issues_count") or 0),
            "watchers": int(it.get("watchers_count") or 0),
            "default_branch": it.get("default_branch"),
            "topics": it.get("topics") or [],
            "last_commit_days": last_commit_days,
        }
        c = Candidate(
            source_id="github",
            source_type="repo",
            title=it.get("name") or full_name,
            url=it.get("html_url") or f"https://github.com/{full_name}",
            external_id=f"github:{full_name}",
            abstract=(it.get("description") or "")[:2000] or None,
            authors=[full_name.split("/")[0]],
            year=(pushed_at.year if pushed_at else None),
            score_raw=extras,
            extras=extras,
        )
        out.append(c)

    log(f"github: {len(out)} repo candidates for q={q!r}")
    return out


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "claude code skill"
    res = asyncio.run(fetch(q, config={"stars_min": 10}, max_items=5))
    for c in res:
        print(f"  {c.external_id} ★{c.extras['stars']} — {c.title}")
