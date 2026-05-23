"""
sources/docs_crawl.py — docs profile source. Bridges to crawl/ dispatcher.

For Stage 1 we keep this thin: take `root_urls` from config, hand them
to `crawl.crawl_url(...)` which tries llms.txt → sitemap → Crawl4AI.

If no root_urls in config but the query looks like a URL/domain, we
treat the query itself as the root.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

from _common import log
from ._base import Candidate


_URL_RE = re.compile(r"https?://[^\s)]+", re.I)


def _extract_root_urls(query: str, config: dict) -> list[str]:
    """Pick root URLs to crawl. Sources, in order:
       1. config['root_urls'] list
       2. URLs embedded in the query string
       3. Single bare-domain token in the query (https://) auto-promoted
    """
    roots = list(config.get("root_urls") or [])
    if not roots:
        for m in _URL_RE.finditer(query):
            url = m.group(0).rstrip(".,);")
            if url not in roots:
                roots.append(url)
    if not roots:
        # bare domain heuristic ("docs.stripe.com api reference")
        tokens = query.split()
        for tok in tokens:
            if "." in tok and not tok.startswith("."):
                bare = tok.strip(",.()<>")
                if "." in bare and "/" not in bare:
                    parsed = urllib.parse.urlparse(f"https://{bare}/")
                    if parsed.netloc and "." in parsed.netloc:
                        roots.append(f"https://{bare}/")
                        break
    return roots


async def fetch(
    query: str,
    *,
    config: dict[str, Any] | None = None,
    max_items: int = 50,
    **_: Any,
) -> list[Candidate]:
    config = dict(config or {})
    roots = _extract_root_urls(query, config)
    if not roots:
        log("docs_crawl: no root URLs (provide config.root_urls or include URLs in query)")
        return []

    max_pages = int(config.get("max_pages") or 50)
    # cap per-source to avoid eating the whole max_items budget
    per_root = max(1, max_items // max(1, len(roots)))

    try:
        from crawl import crawl_url
    except ImportError as e:
        log(f"docs_crawl: crawl module unavailable ({e}); returning empty")
        return []

    import asyncio as _asyncio
    # Crawl roots in parallel rather than sequentially.
    tasks = [
        crawl_url(root, max_pages=min(per_root, max_pages), query_keywords=query)
        for root in roots
    ]
    crawl_results = await _asyncio.gather(*tasks, return_exceptions=True)

    out: list[Candidate] = []
    for root, pages in zip(roots, crawl_results):
        if isinstance(pages, Exception):
            log(f"docs_crawl: crawl of {root} failed: {pages!r}")
            continue
        for p in pages:
            extras = dict(p.get("meta") or {})
            extras.setdefault("root_url", root)
            extras.setdefault("source", p.get("source"))  # llms.txt | sitemap | crawl4ai | trafilatura
            c = Candidate(
                source_id="docs_crawl",
                source_type="doc",
                title=p.get("title") or p.get("url") or "(no title)",
                url=p.get("url") or root,
                external_id=f"docs:{p.get('url') or root}",
                abstract=(p.get("markdown") or p.get("text") or "")[:2000] or None,
                score_raw=extras,
                extras=extras,
            )
            out.append(c)
            if len(out) >= max_items:
                break
        if len(out) >= max_items:
            break

    log(f"docs_crawl: {len(out)} pages from {len(roots)} root(s)")
    return out[:max_items]
