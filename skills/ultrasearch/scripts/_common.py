"""
_common.py — shared helpers across ultrasearch scripts.

Import-safe: no top-level side effects, no I/O. Scripts import what they need
(`from _common import log, json_stdout, normalize_doi, ...`).

Layout (mirrors skills/doc2kb/scripts/_common.py for parity, but trimmed to
the subset ultrasearch actually uses — no image helpers, no docx frontmatter
emitter, no extract args parser).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


SCHEMA_VERSION_DOC = "1.0"

# Rough char-per-token ratios for the heuristic token counter fallback.
# Anglo languages pack ~3.5 chars/token; cyrillic ~2 chars/token in cl100k.
_HEURISTIC_CHARS_PER_TOKEN_ASCII = 3.5
_HEURISTIC_CHARS_PER_TOKEN_NONASCII = 2.0


# ---------- logging ----------

def log(msg: str, prefix: str = "ultrasearch") -> None:
    print(f"[{prefix}] {msg}", file=sys.stderr, flush=True)


# ---------- json io ----------

def json_stdout(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def emit_success(extra: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"ok": True}
    if extra:
        payload.update(extra)
    json_stdout(payload)


def emit_failure(reason: str, extra: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"ok": False, "reason": reason}
    if extra:
        payload.update(extra)
    json_stdout(payload)


# ---------- hashing ----------

def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def short_id(s: str, length: int = 16) -> str:
    return sha256_str(s)[:length]


# ---------- string normalization ----------

def nfc(s: str) -> str:
    """Normalize a string to Unicode NFC form."""
    return unicodedata.normalize("NFC", s)


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s)


def slugify(text: str, maxlen: int = 48) -> str:
    """Compact ASCII slug. Strips diacritics, drops non-ASCII (cyrillic, CJK).
    Falls back to 'doc' if the result is empty."""
    s = unicodedata.normalize("NFKD", text)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    if not s:
        s = "doc"
    return s[:maxlen].rstrip("-") or "doc"


_DOI_PREFIX_RE = re.compile(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", re.I)


def normalize_doi(s: str | None) -> str | None:
    """Lowercase, strip URL prefix ('https://doi.org/', 'doi:'), validate
    that it looks like a Crossref DOI ('10.xxxx/...'). Returns None if not a
    DOI. Used everywhere as the dedup key for papers."""
    if not s:
        return None
    s = _DOI_PREFIX_RE.sub("", s.strip()).lower()
    if not s.startswith("10."):
        return None
    # strip trailing punctuation that often clings when DOIs are extracted
    # from references ("10.1234/foo.", "10.1234/foo);")
    s = s.rstrip(".,);]>")
    return s if s.startswith("10.") else None


_TITLE_WS_RE = re.compile(r"\s+")


def casefold_title(s: str | None) -> str:
    """NFKC + casefold + collapse whitespace runs to a single space. Used as
    fallback dedup key when DOI is missing."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).casefold().strip()
    return _TITLE_WS_RE.sub(" ", s)


# ---------- time ----------

def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- tokens ----------

_TIKTOKEN_ENC = None


def _get_tiktoken_enc():
    global _TIKTOKEN_ENC
    if _TIKTOKEN_ENC is not None:
        return _TIKTOKEN_ENC
    try:
        import tiktoken  # type: ignore
        _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _TIKTOKEN_ENC = False  # cached miss
    return _TIKTOKEN_ENC


def count_tokens(text: str) -> int:
    """Try tiktoken cl100k_base; fall back to a char-based heuristic.
    cl100k_base is a fair proxy for Claude tokenization (5-10% drift)."""
    enc = _get_tiktoken_enc()
    if enc:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii = len(text) - ascii_chars
    return int(
        ascii_chars / _HEURISTIC_CHARS_PER_TOKEN_ASCII
        + non_ascii / _HEURISTIC_CHARS_PER_TOKEN_NONASCII
    )


# ---------- math ----------

def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity for two equal-length vectors. Returns 0 if either
    vector is zero. Used for query-vs-chunk similarity in retrieve.py when
    sqlite-vec returns raw embeddings instead of pre-computed distance."""
    if len(a) != len(b):
        raise ValueError(f"cosine: length mismatch {len(a)} != {len(b)}")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ---------- skill dir resolution ----------

def skill_dir() -> Path:
    """Return the ultrasearch skill root directory (one level above scripts/)."""
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Return the data directory (corpus.db, cache/) — relative to the skill
    root. After install lives at ~/.claude/skills/ultrasearch/data/."""
    return skill_dir() / "data"


def cache_dir(*parts: str) -> Path:
    """Return a cache subdirectory, mkdir -p as needed. Examples:
    cache_dir('pdfs'), cache_dir('api', 'openalex').
    """
    d = data_dir() / "cache"
    for p in parts:
        d = d / p
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- versioning ----------

def tool_version_string() -> str:
    """Best-effort version string. Reads VERSION at repo root if available,
    else returns 'unknown'."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        vfile = parent / "VERSION"
        if vfile.is_file():
            try:
                return vfile.read_text(encoding="utf-8").strip()
            except Exception:
                return "unknown"
        if parent.name in ("skills", "scripts"):
            continue
    return "unknown"


# ---------- env helpers ----------

def require_env(name: str, *, hint: str = "") -> str:
    """Read a required env var; if missing, emit a JSON failure on stdout and
    exit 2. Used by discover.py / fetch.py / index.py at startup."""
    import os
    val = os.environ.get(name)
    if val:
        return val
    msg = f"env var {name!r} is required"
    if hint:
        msg += f" — {hint}"
    emit_failure(msg, {"env_var": name})
    sys.exit(2)


def get_env(name: str, default: str | None = None) -> str | None:
    import os
    val = os.environ.get(name)
    return val if val else default


# ---------- shared endpoints + tunables ----------

# API base URLs reused across discover + traverse (lookups + relations).
S2_BASE = "https://api.semanticscholar.org/graph/v1"
CROSSREF_BASE = "https://api.crossref.org/works"

# Polite-pool rate budgets (requests-per-second targets) keyed by endpoint
# family — research §2 table, ADR-005. Discover and traverse share the same
# throttle bucket per source (the rate limiter sums concurrent in-flight
# requests across the codebase).
RATE_BUDGET: dict[str, float] = {
    "openalex":  10.0,   # research §2 line 26: 100 RPS hard; we stay polite
    "s2":         1.0,   # research §2 line 27: dedicated key = 1 RPS
    "arxiv":      0.33,  # research §2 line 28: 3s between requests
    "crossref":   5.0,   # research §2 line 29: polite ≤50 RPS, stay polite
    "europepmc":  5.0,   # research §2 line 30: conservative
    "core":       0.17,  # research §2 line 32: ~10 req/min free tier
    "unpaywall":  5.0,   # research §2 line 31: soft 100k/day cap
    "datacite":   5.0,
    # Traverse stage subkeys — added so traverse can reach them via the same
    # dict (avoids a separate TRAVERSE_RATE_BUDGET dict that drifted before).
    "s2_refs":    1.0,
    "s2_citers":  1.0,
}

# Baseline year used by recency-aware scorers (quality.recency_score and
# scoring/academic.py). Single source — update when "now" drifts past it.
RECENT_YEAR_BASELINE = 2026


def default_user_agent(email: str | None = None) -> str:
    """Single source for the User-Agent header. Some endpoints (Crossref
    polite pool, OpenAlex) require an email; ``mailto:`` is appended only
    when an email is supplied. Version is read from VERSION via
    ``tool_version_string()`` so a release bump propagates automatically."""
    ver = tool_version_string()
    base = f"ultrasearch/{ver} (+https://github.com/zevtos/agentpipe)"
    if email:
        # Strip the closing paren so we can append the mailto inside.
        return base[:-1] + f"; mailto:{email})"
    return base


def default_http_timeout(read: float = 30.0) -> "Any":
    """Single source for httpx.Timeout(...). Most sources fit the default
    (connect=10, read=30, write=10, pool=10); fetch.py overrides read=60
    for PDF downloads, package-registry sources pass read=20."""
    import httpx
    return httpx.Timeout(connect=10.0, read=read, write=10.0, pool=10.0)
