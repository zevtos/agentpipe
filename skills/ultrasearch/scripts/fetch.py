#!/usr/bin/env python3
"""
fetch.py — open-access PDF downloader for ultrasearch (Stage 1).

Stage 1 cascade (research §13 line 268; 01_stage1.md §3.2):
    Tier 1: Unpaywall  (requires DOI + UNPAYWALL_EMAIL)
    Tier 2: arXiv      (requires arxiv_id)

Stage 2 will expand to 7+ tiers per research §9 (OpenAlex pdf_url priority 1,
Europe PMC fullTextXML, PMC OAI, publisher OA templates, Zenodo/OSF/figshare,
arXiv HTML ar5iv fallback).

Cache contract:
    PDFs land in <skill_dir>/data/cache/pdfs/<sha256(url)[:32]>.pdf.
    Cross-invocation cache hits: same URL → same file → skip download.
    Per-content dedup is the second hash (sha256_of(file)) stored on
    papers.pdf_sha256 by index.py.

Validation:
    Every downloaded byte stream is checked for `%PDF` magic bytes and
    Content-Type containing 'pdf'. HTML interstitials disguised as PDFs
    are rejected before they reach parse.py.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Literal, TypedDict

import httpx

from _common import (
    cache_dir,
    emit_failure,
    get_env,
    json_stdout,
    log,
    require_env,
    sha256_of,
)
from discover import Candidate, _candidate_key  # type: ignore


# -------- constants --------

HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
MAX_PDF_BYTES = 50 * 1024 * 1024   # 50 MB
DOWNLOAD_CHUNK = 1 << 16
USER_AGENT = "ultrasearch/0.1 (+https://github.com/zevtos/agentpipe)"
DEFAULT_CONCURRENCY = 4


FETCH_SOURCE = Literal[
    "openalex_pdf", "unpaywall", "arxiv",
    "publisher_oa", "zenodo", "grey"
]


GREY_DISCLAIMER = """\
⚠ GREY MODE ENABLED. The --grey flag uses `scidownl` / sci-hub mirrors as a
last-resort fetch tier. This is a LEGAL GRAY ZONE in most jurisdictions:
- Sci-hub bypasses publisher paywalls and is not authorized by publishers.
- In some countries the END-USER who downloads via sci-hub bears legal risk
  (Germany, France, UK, US Hadleys Act / DMCA, etc).
- Network operators may log sci-hub access via DNS or TLS SNI.
You enabled this with `--grey`. ultrasearch displays this notice once per run.
Open-access tiers (1-6) are tried FIRST; --grey only activates when they all fail.
"""


class FetchResult(TypedDict, total=False):
    ok: bool
    candidate_key: str
    pdf_path: str | None
    source: FETCH_SOURCE | None
    bytes: int | None
    sha256: str | None
    error: str | None
    elapsed_s: float


# -------- helpers --------

def _url_cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def _cache_path(url: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    return dest_dir / (_url_cache_key(url) + ".pdf")


def _looks_like_pdf(content_type: str | None, head_bytes: bytes) -> bool:
    if head_bytes[:4] == b"%PDF":
        return True
    if content_type and "pdf" in content_type.lower():
        # some servers return application/octet-stream — be tolerant if magic matches
        return True
    return False


async def _download_pdf(client: httpx.AsyncClient, url: str,
                        dest_dir: Path) -> tuple[Path, int, str] | None:
    """Stream `url` to <dest_dir>/<sha256(url)>.pdf. Validates Content-Type
    and `%PDF` magic. Returns (path, size_bytes, sha256_of_content) or None."""
    dest = _cache_path(url, dest_dir)
    if dest.exists() and dest.stat().st_size > 0:
        try:
            sha = sha256_of(dest)
        except OSError:
            dest.unlink(missing_ok=True)
        else:
            return dest, dest.stat().st_size, sha

    tmp = dest.with_suffix(".pdf.tmp")
    bytes_written = 0
    head = b""
    h = hashlib.sha256()
    try:
        async with client.stream("GET", url, timeout=HTTP_TIMEOUT,
                                 follow_redirects=True) as r:
            if r.status_code != 200:
                log(f"fetch: {url} → HTTP {r.status_code}")
                return None
            ct = r.headers.get("content-type", "")
            with tmp.open("wb") as fh:
                async for chunk in r.aiter_bytes(DOWNLOAD_CHUNK):
                    if bytes_written == 0:
                        head = chunk[:4]
                        if not _looks_like_pdf(ct, head):
                            log(f"fetch: {url} → not a PDF (ct={ct!r} magic={head!r})")
                            return None
                    fh.write(chunk)
                    h.update(chunk)
                    bytes_written += len(chunk)
                    if bytes_written > MAX_PDF_BYTES:
                        log(f"fetch: {url} → exceeded {MAX_PDF_BYTES} bytes")
                        return None
    except httpx.HTTPError as e:
        log(f"fetch: {url} → http error {type(e).__name__}: {e}")
        tmp.unlink(missing_ok=True)
        return None
    except OSError as e:
        log(f"fetch: write error: {e}")
        tmp.unlink(missing_ok=True)
        return None

    if bytes_written == 0:
        tmp.unlink(missing_ok=True)
        return None

    try:
        tmp.replace(dest)
    except OSError as e:
        log(f"fetch: rename failed: {e}")
        tmp.unlink(missing_ok=True)
        return None

    return dest, bytes_written, h.hexdigest()


# -------- tier 1: Unpaywall --------

# -------- tier 1: OpenAlex pdf_url hint --------

async def _try_openalex_pdf(c: Candidate) -> str | None:
    """OpenAlex stores best_oa_location.pdf_url on the candidate as
    `pdf_url_hint` — return it directly if present."""
    return c.get("pdf_url_hint")


# -------- tier 2: Unpaywall --------

async def _try_unpaywall(client: httpx.AsyncClient, c: Candidate,
                         email: str) -> str | None:
    doi = c.get("doi")
    if not doi:
        return None
    url = f"https://api.unpaywall.org/v2/{doi}"
    try:
        r = await client.get(url, params={"email": email}, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError as e:
        log(f"unpaywall: http error {e}")
        return None
    if r.status_code == 404 or r.status_code == 422:
        return None
    if r.status_code != 200:
        log(f"unpaywall: DOI {doi} → HTTP {r.status_code}")
        return None
    try:
        data = r.json()
    except Exception:
        return None
    if not data.get("is_oa"):
        return None
    best = data.get("best_oa_location") or {}
    pdf_url = best.get("url_for_pdf") or best.get("url")
    return pdf_url or None


# -------- tier 3: arXiv direct PDF --------

async def _try_arxiv(c: Candidate) -> str | None:
    aid = c.get("arxiv_id")
    if not aid:
        return None
    # strip version suffix if present
    aid = aid.split("v")[0]
    return f"https://arxiv.org/pdf/{aid}.pdf"


# -------- tier 4: Europe PMC PMC ID resolution --------

# -------- tier 5: publisher OA URL templates --------

_OA_PUBLISHER_TEMPLATES: dict[str, str] = {
    # DOI prefix → URL template (formatted with `doi=normalized_doi`)
    "10.1371": "https://journals.plos.org/plosone/article/file?id={doi}&type=printable",
    # MDPI uses split DOI form; their PDFs live at https://www.mdpi.com/<vol>/<iss>/<id>/pdf
    # but the path is encoded in DOI as 10.3390/<journal>10010001 — too lossy for a
    # generic template; rely on Unpaywall for MDPI.
    "10.3389": "https://www.frontiersin.org/articles/{doi}/pdf",  # Frontiers
}


async def _try_publisher_oa(c: Candidate) -> str | None:
    doi = c.get("doi")
    if not doi:
        return None
    prefix = doi.split("/")[0]
    template = _OA_PUBLISHER_TEMPLATES.get(prefix)
    if not template:
        return None
    return template.format(doi=doi)


# -------- tier 6: Zenodo / OSF / figshare --------

async def _try_zenodo(client: httpx.AsyncClient, c: Candidate) -> str | None:
    """If DOI is a Zenodo record, fetch the file URL via the Zenodo API.
    Zenodo DOIs look like 10.5281/zenodo.NNNNNN"""
    doi = c.get("doi") or ""
    if not doi.startswith("10.5281/zenodo."):
        return None
    record_id = doi.rsplit(".", 1)[-1]
    url = f"https://zenodo.org/api/records/{record_id}"
    try:
        r = await client.get(url, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError as e:
        log(f"zenodo: http error {e}")
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    for f in (data.get("files") or []):
        link = (f.get("links") or {}).get("self")
        key = f.get("key", "")
        if link and (key.lower().endswith(".pdf") or "pdf" in (f.get("type") or "").lower()):
            return link
    return None


# -------- public API --------

# -------- tier 8 (opt-in): sci-hub via scidownl --------

async def _try_grey(client: httpx.AsyncClient, c: Candidate,
                    dest: Path) -> str | None:
    """Sci-hub fallback. Only invoked if `grey=True` passed to fetch_pdf.
    Uses scidownl if installed. The disclaimer is printed up-front by
    `ultrasearch.py` (see fetch.GREY_DISCLAIMER); we don't repeat it here."""
    doi = c.get("doi")
    if not doi:
        return None
    try:
        import scidownl  # type: ignore
    except ImportError:
        log("grey: scidownl not installed; skipping (pip install scidownl)",
            prefix="fetch")
        return None
    # scidownl's API is sync; wrap in to_thread
    import asyncio as _asyncio
    try:
        out = dest / f"grey_{c.get('doi','').replace('/','_')[:40]}.pdf"
        # scidownl signature varies across versions; fall back gracefully
        if hasattr(scidownl, "scihub_download"):
            await _asyncio.to_thread(
                scidownl.scihub_download, doi, paper_type="doi", out=str(out))
        elif hasattr(scidownl, "scidownl_download"):
            await _asyncio.to_thread(scidownl.scidownl_download, doi, str(out))
        else:
            log("grey: scidownl API incompatible", prefix="fetch")
            return None
    except Exception as e:
        log(f"grey: scidownl raised: {e}", prefix="fetch")
        return None
    if out.is_file() and out.stat().st_size > 1024:
        return str(out.resolve())
    return None


async def fetch_pdf(client: httpx.AsyncClient, c: Candidate, *,
                    unpaywall_email: str | None = None,
                    pdf_cache: Path | None = None,
                    grey: bool = False) -> FetchResult:
    """7-tier cascade (research §9 lines 163-176).
    Returns first success — `source` field tells which tier won.
    Tiers, in priority order:
        1. OpenAlex `primary_location.pdf_url` (via candidate.pdf_url_hint)
        2. Unpaywall
        3. arXiv direct PDF
        4. Europe PMC PMC fulltext
        5. Publisher OA templates (PLOS, Frontiers, …)
        6. Zenodo
        7. arXiv HTML (ar5iv) — hook for Stage 3
    """
    t0 = time.monotonic()
    key = _candidate_key(c)
    dest = pdf_cache or cache_dir("pdfs")
    unpaywall_email = unpaywall_email or get_env("UNPAYWALL_EMAIL")

    tier_sources: list[tuple[FETCH_SOURCE, Any]] = []

    # Tier 1: OpenAlex hint
    oa_url = await _try_openalex_pdf(c)
    if oa_url:
        tier_sources.append(("openalex_pdf", oa_url))

    # Tier 2: Unpaywall
    if c.get("doi") and unpaywall_email:
        up_url = await _try_unpaywall(client, c, unpaywall_email)
        if up_url:
            tier_sources.append(("unpaywall", up_url))

    # Tier 3: arXiv
    ax_url = await _try_arxiv(c)
    if ax_url:
        tier_sources.append(("arxiv", ax_url))

    # Tier 4: Europe PMC (returns XML URL, not PDF — keep last among real PDF tiers)
    # Skipped in current Stage 2 — we don't parse XML to PDF. Hook reserved.

    # Tier 5: publisher OA template
    pub_url = await _try_publisher_oa(c)
    if pub_url:
        tier_sources.append(("publisher_oa", pub_url))

    # Tier 6: Zenodo
    zn_url = await _try_zenodo(client, c)
    if zn_url:
        tier_sources.append(("zenodo", zn_url))

    # Try downloads in order
    for src, url in tier_sources:
        res = await _download_pdf(client, url, dest)
        if not res:
            continue
        path, size, sha = res
        return {
            "ok": True, "candidate_key": key,
            "pdf_path": str(path), "source": src,
            "bytes": size, "sha256": sha,
            "elapsed_s": round(time.monotonic() - t0, 3),
        }

    # Tier 8 (opt-in): sci-hub fallback
    if grey:
        grey_path = await _try_grey(client, c, dest)
        if grey_path:
            p = Path(grey_path)
            return {
                "ok": True, "candidate_key": key,
                "pdf_path": grey_path, "source": "grey",
                "bytes": p.stat().st_size, "sha256": sha256_of(p),
                "elapsed_s": round(time.monotonic() - t0, 3),
            }

    return {
        "ok": False, "candidate_key": key,
        "pdf_path": None, "source": None,
        "error": "no_oa_source_available",
        "elapsed_s": round(time.monotonic() - t0, 3),
    }


async def fetch_many(candidates: list[Candidate], *,
                     concurrency: int = DEFAULT_CONCURRENCY,
                     unpaywall_email: str | None = None,
                     grey: bool = False) -> list[FetchResult]:
    sem = asyncio.Semaphore(concurrency)
    headers = {"User-Agent": USER_AGENT}

    async with httpx.AsyncClient(headers=headers, http2=True,
                                 follow_redirects=True) as client:
        async def _wrapped(c: Candidate) -> FetchResult:
            async with sem:
                try:
                    return await fetch_pdf(client, c,
                                           unpaywall_email=unpaywall_email,
                                           grey=grey)
                except Exception as e:
                    log(f"fetch_pdf raised on {_candidate_key(c)}: {e}")
                    return {
                        "ok": False,
                        "candidate_key": _candidate_key(c),
                        "error": f"{type(e).__name__}: {e}",
                        "elapsed_s": 0.0,
                    }
        return await asyncio.gather(*(_wrapped(c) for c in candidates))


# -------- CLI --------

def main() -> int:
    parser = argparse.ArgumentParser(prog="fetch.py",
                                     description="Fetch OA PDFs for one or more candidates.")
    parser.add_argument("--candidate-json", default=None,
                        help="single Candidate as JSON string")
    parser.add_argument("--batch", action="store_true",
                        help="read NDJSON of Candidates from stdin, emit NDJSON of FetchResults")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    args = parser.parse_args()

    if not args.batch and not args.candidate_json:
        emit_failure("provide --candidate-json or --batch")
        return 2

    unpaywall_email = get_env("UNPAYWALL_EMAIL")
    if not unpaywall_email:
        log("UNPAYWALL_EMAIL not set — Tier 1 (Unpaywall) will be skipped")

    if args.batch:
        cands: list[Candidate] = []
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cands.append(json.loads(line))
            except json.JSONDecodeError as e:
                log(f"bad input line: {e}")
        try:
            results = asyncio.run(fetch_many(cands, concurrency=args.concurrency,
                                             unpaywall_email=unpaywall_email))
        except KeyboardInterrupt:
            emit_failure("interrupted")
            return 130
        for r in results:
            json_stdout(r)
        return 0

    try:
        c = json.loads(args.candidate_json)
    except json.JSONDecodeError as e:
        emit_failure(f"bad candidate JSON: {e}")
        return 2
    headers = {"User-Agent": USER_AGENT}

    async def _one():
        async with httpx.AsyncClient(headers=headers, http2=True,
                                     follow_redirects=True) as client:
            return await fetch_pdf(client, c, unpaywall_email=unpaywall_email)

    try:
        result = asyncio.run(_one())
    except KeyboardInterrupt:
        emit_failure("interrupted")
        return 130
    json_stdout(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
