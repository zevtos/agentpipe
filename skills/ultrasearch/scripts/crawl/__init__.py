"""
crawl — pre-flight + deep-crawl dispatcher for the docs profile.

Cascade per Stage 1:
  1. /llms.txt or /llms-full.txt — fetch + parse markdown for a curated URL list
  2. /sitemap.xml — fall through if no llms.txt; parse and fetch each URL
  3. Crawl4AI BFS deep-crawl — fall through if no sitemap (lazy-imported,
     opt-in via deps in profiles/docs.yaml)
  4. trafilatura.spider — last-resort static crawler

`crawl_url(root, max_pages, query_keywords)` returns a list of page dicts:
  {url, title, markdown, text, source, meta}
where `source` ∈ {llms.txt, sitemap, crawl4ai, trafilatura}.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from _common import default_http_timeout, log


_TIMEOUT = default_http_timeout(read=20.0)
_USER_AGENT = "ultrasearch/0.15"

_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.I)
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.I | re.S)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.I | re.S)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Cloud-metadata + link-local IPs that must never be reached.
_FORBIDDEN_HOSTS: frozenset[str] = frozenset({
    "metadata.google.internal",
    "metadata.goog",
    "169.254.169.254",
    "fd00:ec2::254",
})


def _url_is_safe(url: str) -> bool:
    """Reject SSRF-class URLs: non-http(s) schemes, private/loopback/link-local
    IPs, cloud-metadata hosts. Resolves DNS once and rejects any private IP."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in _FORBIDDEN_HOSTS:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False
    return True


async def _try_fetch(url: str, *, client: httpx.AsyncClient) -> str | None:
    if not _url_is_safe(url):
        log(f"crawl: refusing unsafe URL {url}")
        return None
    try:
        resp = await client.get(url, headers={"User-Agent": _USER_AGENT})
    except httpx.HTTPError as e:
        log(f"crawl: fetch {url} failed: {type(e).__name__}")
        return None
    if resp.status_code != 200:
        return None
    return resp.text


async def _fetch_one_page(
    client: httpx.AsyncClient, url: str, source: str
) -> dict[str, Any] | None:
    text = await _try_fetch(url, client=client)
    if not text:
        return None
    # Strip HTML for a quick text representation. Markdown extraction is the
    # job of trafilatura/crawl4ai; the pre-flight stages just give us text.
    title_m = _TITLE_RE.search(text)
    title = (title_m.group(1).strip() if title_m else url)
    body = _SCRIPT_RE.sub(" ", text)
    body = _STYLE_RE.sub(" ", body)
    body = _HTML_TAG_RE.sub(" ", body)
    body = _WS_RE.sub(" ", body).strip()
    return {
        "url": url,
        "title": title[:200],
        "markdown": None,
        "text": body[:4000],
        "source": source,
        "meta": {"length": len(body)},
    }


async def _via_llms_txt(
    client: httpx.AsyncClient, root: str, max_pages: int
) -> list[dict[str, Any]] | None:
    """Stage 1: pre-flight llms.txt / llms-full.txt parse."""
    from .llms_txt import parse_llms_txt
    candidates = [urljoin(root, "/llms-full.txt"), urljoin(root, "/llms.txt")]
    for u in candidates:
        text = await _try_fetch(u, client=client)
        if text:
            urls = parse_llms_txt(text, base=root)
            if not urls:
                continue
            log(f"crawl: llms.txt at {u} -> {len(urls)} URLs")
            urls = urls[:max_pages]
            tasks = [_fetch_one_page(client, page_url, "llms.txt") for page_url in urls]
            pages = await asyncio.gather(*tasks, return_exceptions=True)
            return [p for p in pages if isinstance(p, dict)]
    return None


async def _via_sitemap(
    client: httpx.AsyncClient, root: str, max_pages: int
) -> list[dict[str, Any]] | None:
    """Stage 2: sitemap.xml fall-through."""
    from .sitemap import parse_sitemap
    candidates = [urljoin(root, "/sitemap.xml"), urljoin(root, "/sitemap_index.xml")]
    for u in candidates:
        text = await _try_fetch(u, client=client)
        if not text:
            continue
        urls = parse_sitemap(text, base=root)
        if not urls:
            continue
        log(f"crawl: sitemap at {u} -> {len(urls)} URLs (taking first {max_pages})")
        urls = urls[:max_pages]
        tasks = [_fetch_one_page(client, page_url, "sitemap") for page_url in urls]
        pages = await asyncio.gather(*tasks, return_exceptions=True)
        return [p for p in pages if isinstance(p, dict)]
    return None


def _crawl4ai_available() -> bool:
    try:
        import crawl4ai  # noqa: F401
        return True
    except ImportError:
        return False


async def _via_crawl4ai(
    root: str, max_pages: int, query_keywords: str | None
) -> list[dict[str, Any]] | None:
    if not _crawl4ai_available():
        log("crawl: crawl4ai not installed (run ensure_env.py:ensure_crawl4ai)")
        return None
    try:
        from .crawl4ai_helpers import bfs_deep_crawl
        return await bfs_deep_crawl(root, max_pages=max_pages, query_keywords=query_keywords)
    except Exception as e:
        log(f"crawl: crawl4ai failed: {e!r}")
        return None


async def crawl_url(
    root_url: str,
    *,
    max_pages: int = 50,
    query_keywords: str | None = None,
) -> list[dict[str, Any]]:
    """Top-level dispatcher. Tries llms.txt → sitemap → crawl4ai in order
    and returns the first non-empty result list."""
    parsed = urlparse(root_url)
    if not parsed.scheme:
        root_url = f"https://{root_url.lstrip('/')}"
    # Normalize to root domain for llms.txt / sitemap lookup
    root_origin = f"{urlparse(root_url).scheme}://{urlparse(root_url).netloc}"

    if not _url_is_safe(root_origin):
        log(f"crawl: refusing unsafe root {root_origin}")
        return []

    # follow_redirects=False — redirects bypass _url_is_safe(); we re-validate
    # per-fetch instead.
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        # 1
        pages = await _via_llms_txt(client, root_origin, max_pages)
        if pages:
            return pages
        # 2
        pages = await _via_sitemap(client, root_origin, max_pages)
        if pages:
            return pages
    # 3 (uses its own client internally)
    pages = await _via_crawl4ai(root_url, max_pages, query_keywords)
    if pages:
        return pages
    log(f"crawl: all strategies returned empty for {root_url}")
    return []


__all__ = ["crawl_url"]
