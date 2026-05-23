"""
crawl/llms_txt.py — parse `/llms.txt` / `/llms-full.txt` markdown.

Format (per llmstxt.org):
  H2 sections group links; each link is `- [Label](url): description`.
  Top of file has an H1 title + optional blockquote summary.

We don't fully respect the spec — we just extract URLs.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def parse_llms_txt(text: str, *, base: str | None = None) -> list[str]:
    """Return ordered, deduplicated list of URLs extracted from an llms.txt body.

    `base` is used to resolve any relative links (rare in llms.txt but handled).
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(">"):
            continue
        for _, url in _LINK_RE.findall(line):
            url = url.rstrip(".,);")
            if base and not url.startswith(("http://", "https://")):
                url = urljoin(base, url)
            if url and url not in seen:
                seen.add(url)
                out.append(url)
    return out
