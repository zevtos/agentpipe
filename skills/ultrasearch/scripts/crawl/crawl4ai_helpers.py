"""
crawl/crawl4ai_helpers.py — BFS deep-crawl wrapper around Crawl4AI.

Lazy-imported by crawl/__init__.py only when llms.txt + sitemap both fail.
crawl4ai needs Playwright (~300MB cold install); ensure_env.py handles
the lazy install path.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from _common import log


async def bfs_deep_crawl(
    root_url: str,
    *,
    max_pages: int = 50,
    query_keywords: str | None = None,
) -> list[dict[str, Any]]:
    """BFS deep crawl with same-domain filter + keyword-relevance scorer.

    Imports crawl4ai lazily so a missing install raises ImportError that
    the dispatcher swallows + logs.
    """
    from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode
    from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
    from crawl4ai.deep_crawling.filters import FilterChain, DomainFilter
    from crawl4ai.deep_crawling.scorers import KeywordRelevanceScorer

    domain = urlparse(root_url).netloc
    scorer = None
    if query_keywords:
        # Split query into ~5 keyword tokens for the relevance scorer
        kws = [t for t in query_keywords.lower().split() if len(t) > 2][:5]
        if kws:
            scorer = KeywordRelevanceScorer(keywords=kws, weight=0.7)

    config = CrawlerRunConfig(
        deep_crawl_strategy=BFSDeepCrawlStrategy(
            max_depth=3,
            max_pages=max_pages,
            filter_chain=FilterChain([DomainFilter(allowed_domains=[domain])]),
            url_scorer=scorer,
            score_threshold=0.0,
        ),
        cache_mode=CacheMode.ENABLED,
    )

    pages: list[dict[str, Any]] = []
    try:
        async with AsyncWebCrawler() as crawler:
            results = await crawler.arun(url=root_url, config=config)
    except Exception as e:
        log(f"crawl4ai: arun failed: {e!r}")
        return []

    # crawl4ai 0.6+ returns a list of CrawlResult-like objects
    iterable = results if isinstance(results, list) else [results]
    for r in iterable:
        url = getattr(r, "url", None) or ""
        if not url:
            continue
        md = getattr(r, "markdown", None) or ""
        title = getattr(r, "title", None) or url
        pages.append({
            "url": url,
            "title": title,
            "markdown": md,
            "text": md,
            "source": "crawl4ai",
            "meta": {"score": getattr(r, "score", None)},
        })

    log(f"crawl4ai: {len(pages)} pages crawled from {root_url}")
    return pages[:max_pages]
