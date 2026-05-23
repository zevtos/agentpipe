"""
crawl/sitemap.py — extract <loc> URLs from a sitemap.

Implementation note: we deliberately do NOT use `xml.etree.ElementTree`.
The stdlib docs explicitly warn it is "not secure against maliciously
constructed data" — attacker-controlled sitemap.xml could trigger XXE
or quadratic-blowup variants. A regex over `<loc>...</loc>` is sufficient
for this Stage 1 use case (we only need URL extraction, not validation).

Adding `defusedxml` would be the proper hardening, but it's a profile-specific
dep and not worth the install footprint for this read-only extraction.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

_LOC_RE = re.compile(r"<loc[^>]*>\s*([^<\s][^<]*?)\s*</loc>", re.I)


def parse_sitemap(text: str, *, base: str | None = None) -> list[str]:
    """Extract URLs from a sitemap XML body via regex (XXE-safe).

    Returns ordered, deduplicated URLs. Handles both regular sitemaps and
    sitemap-index files (any `<loc>` text is treated as a URL).
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _LOC_RE.finditer(text):
        url = m.group(1).strip()
        if not url:
            continue
        if base and not url.startswith(("http://", "https://")):
            url = urljoin(base, url)
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out
