"""
_common.py — shared helpers across doc2kb scripts.

Everything here is import-safe: no top-level side effects, no I/O. Scripts
import what they need (`from _common import write_md, count_tokens, ...`).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION_DOC = "1.0"

# Rough char-per-token ratios for the heuristic token counter fallback.
# Anglo languages pack ~3.5 chars/token; cyrillic ~2 chars/token in cl100k.
_HEURISTIC_CHARS_PER_TOKEN_ASCII = 3.5
_HEURISTIC_CHARS_PER_TOKEN_NONASCII = 2.0


# ---------- logging ----------

def log(msg: str, prefix: str = "doc2kb") -> None:
    print(f"[{prefix}] {msg}", file=sys.stderr, flush=True)


# ---------- json io ----------

def json_stdout(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


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


# ---------- slug ----------

def slugify(text: str, maxlen: int = 48) -> str:
    """Compact ASCII slug. Strips diacritics, drops non-ASCII (cyrillic, CJK).
    Falls back to 'doc' if the result is empty."""
    s = unicodedata.normalize("NFKD", text)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    if not s:
        s = "doc"
    return s[:maxlen].rstrip("-") or "doc"


def kb_doc_filename(doc_id: str, source_path: str) -> str:
    """Returns <doc_id>-<slug>.md given a source path. The slug uses
    Path.stem so 'Pro_GOST.pdf' → 'pro-gost'."""
    stem = Path(source_path).stem
    return f"{doc_id}-{slugify(stem)}.md"


_HEADING_BAD_CHARS_RE = re.compile(r"[\r\n\t\x00-\x08\x0b\x0c\x0e-\x1f]+")


def sanitize_heading(text: str, maxlen: int = 200) -> str:
    """Collapse whitespace and strip control chars from a heading captured
    out of a source document. Used by every extract_*.py before recording
    headings in frontmatter or in a [page N]/## Slide heading. Defends
    against attacker-controlled headings injecting newlines that could
    break out of YAML scalars or markdown structure."""
    if not text:
        return ""
    s = _HEADING_BAD_CHARS_RE.sub(" ", text)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:maxlen]


def nfc(s: str) -> str:
    """Normalize a string to Unicode NFC form. Used for `source_path` /
    `source` fields so that the same filename produced by macOS HFS+/APFS
    (which exposes NFD) and the same string written into frontmatter or
    matched by build_manifest agree byte-for-byte. Without this, a Cyrillic
    or accented filename round-trips as NFD from `pathlib` but as NFC from
    YAML frontmatter, and merge_scout sees a false-positive "missing
    extraction" because the two strings differ even though they look
    identical."""
    return unicodedata.normalize("NFC", s)


def validate_source_rel(s: str) -> str:
    """Reject `--source-rel` arguments that try to escape the corpus root.
    Returns the cleaned, NFC-normalized string or raises ValueError.

    Rules:
      - non-empty
      - not absolute (no leading '/' on POSIX, no drive letter on Windows)
      - no '..' components
      - no NUL bytes
      - returned string is NFC-normalized (see `nfc()` for rationale)
    """
    if not s:
        raise ValueError("source-rel is empty")
    if "\x00" in s:
        raise ValueError("source-rel contains NUL byte")
    p = Path(s)
    if p.is_absolute() or (len(s) >= 2 and s[1] == ":"):
        raise ValueError(f"source-rel must be relative, got: {s!r}")
    if any(part == ".." for part in p.parts):
        raise ValueError(f"source-rel must not contain '..': {s!r}")
    return nfc(s)


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
    # heuristic fallback
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii = len(text) - ascii_chars
    return int(
        ascii_chars / _HEURISTIC_CHARS_PER_TOKEN_ASCII
        + non_ascii / _HEURISTIC_CHARS_PER_TOKEN_NONASCII
    )


# ---------- frontmatter ----------

def _yaml_scalar(v: Any) -> str:
    """Minimal YAML scalar serializer — handles str, int, float, bool, None,
    and lists of primitives. Strings get quoted when they contain special
    characters; everything else stays unquoted to keep the file readable.
    We intentionally don't depend on PyYAML — frontmatter is simple.

    SECURITY: any string with control characters (newline, tab, CR, NUL)
    forces the containing list into block style — never an inline list with
    a block scalar inside, which would produce structurally broken YAML."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int) or isinstance(v, float):
        return str(v)
    if isinstance(v, list):
        if not v:
            return "[]"
        if all(isinstance(x, (int, float, bool)) or x is None for x in v):
            return "[" + ", ".join(_yaml_scalar(x) for x in v) + "]"
        # If any string contains control chars or YAML structural chars,
        # force block style — embedding a "|" block scalar inside an inline
        # "[a, b, c]" list is malformed and lets attackers inject siblings.
        force_block = any(
            isinstance(x, str) and _has_block_triggers(x) for x in v
        )
        # Strings → quoted, comma-separated. Long lists go block-style.
        if (not force_block
                and len(v) <= 6
                and all(isinstance(x, str) and len(x) < 40 for x in v)):
            return "[" + ", ".join(_yaml_quote(x, inline=True) for x in v) + "]"
        return "\n" + "\n".join(
            f"  - {_yaml_quote(str(x), inline=True)}" for x in v
        )
    if isinstance(v, str):
        return _yaml_quote(v)
    return _yaml_quote(str(v))


_SAFE_YAML_RE = re.compile(r"^[A-Za-z0-9_./@:+\- ]+$")
_BLOCK_TRIGGERS_RE = re.compile(r"[\r\n\t\x00-\x08\x0b\x0c\x0e-\x1f]")


def _has_block_triggers(s: str) -> bool:
    """True if the string contains control characters that would break an
    inline YAML representation."""
    return bool(_BLOCK_TRIGGERS_RE.search(s))


def _yaml_quote(s: str, inline: bool = False) -> str:
    """Quote a string safely for YAML frontmatter.

    `inline=True` is set by list-item callers — those locations cannot
    contain a block scalar (`|`), so newlines are always represented via
    `\\n` escapes inside a double-quoted scalar. `inline=False` (default,
    used for top-level scalars) may emit a block scalar for true multi-line
    values.
    """
    if s == "":
        return '""'
    # Drop NUL outright — never legitimate in frontmatter, and YAML quoting
    # rules around it are inconsistent.
    s = s.replace("\x00", "")
    has_block_chars = _has_block_triggers(s)
    if has_block_chars and not inline:
        body = "\n".join("  " + line for line in s.split("\n"))
        return "|\n" + body
    if has_block_chars and inline:
        # Inline location — escape control chars inside a double-quoted scalar.
        escaped = (
            s.replace("\\", "\\\\")
             .replace('"', '\\"')
             .replace("\r", "\\r")
             .replace("\n", "\\n")
             .replace("\t", "\\t")
        )
        # Strip any remaining C0 controls (Python YAML readers may reject them).
        escaped = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", escaped)
        return '"' + escaped + '"'
    if (_SAFE_YAML_RE.match(s)
            and s[0] not in "-?:"
            and s[-1] not in ":"
            and "  " not in s):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_frontmatter(d: dict[str, Any]) -> str:
    """Render a Python dict as a YAML frontmatter block including the
    leading and trailing '---' lines plus a trailing newline."""
    lines = ["---"]
    for k, v in d.items():
        rendered = _yaml_scalar(v)
        if rendered.startswith("\n") or rendered.startswith("|"):
            lines.append(f"{k}:{rendered}")
        else:
            lines.append(f"{k}: {rendered}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------- markdown body utilities ----------

_MULTI_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+(?=\n)")


def clean_whitespace(text: str) -> str:
    """Light whitespace cleanup: trim trailing spaces, collapse 3+ blank
    lines to 2, normalize CRLF → LF. Does NOT collapse leading indentation
    (preserves code blocks and lists)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING_WS.sub("", text)
    text = _MULTI_BLANK_LINES.sub("\n\n", text)
    return text.strip() + "\n"


# ---------- file writer ----------

def write_md(out_path: Path, frontmatter: dict[str, Any], body: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fm = render_frontmatter(frontmatter)
    body = clean_whitespace(body)
    out_path.write_text(fm + body, encoding="utf-8")


# ---------- frontmatter reader ----------

_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)

# Keys whose values should never be coerced from string → int/float, even if
# they look numeric. Protects sha256 hashes (all-digit edge case),
# semver-ish version strings, dates, and identifiers from accidental
# narrowing on round-trip.
_STRING_KEYS = frozenset({
    "id", "source", "source_sha256", "extraction_method",
    "extraction_date", "source_encoding", "original_title",
})
_STRICT_NUM_RE = re.compile(r"^-?\d+$|^-?\d+\.\d+$")


def _split_inline_yaml_list(inner: str) -> list[str]:
    """Split `a, "b, c", d` into ['a', '"b, c"', 'd'] — comma-aware splitter
    that respects double-quoted segments (with `\\"` escapes)."""
    items: list[str] = []
    cur: list[str] = []
    in_quote = False
    escape = False
    for ch in inner:
        if escape:
            cur.append(ch)
            escape = False
            continue
        if in_quote and ch == "\\":
            cur.append(ch)
            escape = True
            continue
        if ch == '"':
            cur.append(ch)
            in_quote = not in_quote
            continue
        if ch == "," and not in_quote:
            items.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    tail = "".join(cur).strip()
    if tail or items:  # don't emit a single empty item for ''
        items.append(tail)
    return items


def read_frontmatter(path: Path) -> dict[str, Any]:
    """Lenient YAML frontmatter parser — single line `key: value` pairs only,
    plus block lists with `  - item` syntax. Matches what render_frontmatter
    emits. Returns {} if no frontmatter found."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    return parse_frontmatter_text(text)


def parse_frontmatter_text(text: str) -> dict[str, Any]:
    """Parse frontmatter from an already-loaded string. Used both by
    read_frontmatter and by extract_md_txt when the source carries its own
    YAML frontmatter block to be stripped."""
    # Normalize CRLF so the anchored regex matches Windows-authored sources too.
    if "\r\n" in text[:200]:
        text = text.replace("\r\n", "\n")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    block = m.group(1)
    out: dict[str, Any] = {}
    cur_key: str | None = None
    cur_list: list[str] | None = None
    for raw in block.split("\n"):
        if raw.startswith("  - "):
            if cur_list is None:
                cur_list = []
                if cur_key is not None:
                    out[cur_key] = cur_list
            cur_list.append(_yaml_unquote(raw[4:].strip()))
            continue
        if ":" not in raw:
            cur_list = None
            cur_key = None
            continue
        key, _, val = raw.partition(":")
        key = key.strip()
        val = val.strip()
        cur_key = key
        cur_list = None
        if val == "":
            out[key] = []
            cur_list = out[key]  # type: ignore
            continue
        if val == "null":
            out[key] = None
        elif val == "true":
            out[key] = True
        elif val == "false":
            out[key] = False
        elif val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            if inner == "":
                out[key] = []
            else:
                items = _split_inline_yaml_list(inner)
                out[key] = [_yaml_unquote(x) for x in items]
        elif key not in _STRING_KEYS and _STRICT_NUM_RE.match(val):
            try:
                out[key] = float(val) if "." in val else int(val)
            except ValueError:
                out[key] = _yaml_unquote(val)
        else:
            out[key] = _yaml_unquote(val)
    return out


def _yaml_unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1]
    return s


def read_body(path: Path) -> str:
    """Returns the markdown body after the frontmatter (or full text if no
    frontmatter)."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return text
    return text[m.end():]


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


# ---------- common arg parser snippet ----------

def common_extract_args(parser):
    parser.add_argument("input_file", help="Path to the source file")
    parser.add_argument("output_md", help="Path to the output .md file")
    parser.add_argument("--doc-id", default="doc-000",
                        help="Document id used in frontmatter (default: doc-000)")
    parser.add_argument("--source-rel", default=None,
                        help="Source path relative to corpus root for frontmatter "
                             "(default: basename of input_file)")
    return parser


def emit_success(out_path: Path, body: str, warnings: Iterable[str] = (),
                 extra: dict[str, Any] | None = None) -> None:
    """Print canonical success JSON to stdout."""
    payload = {
        "ok": True,
        "out": str(out_path),
        "tokens_estimated": count_tokens(body),
        "warnings": list(warnings),
    }
    if extra:
        payload.update(extra)
    json_stdout(payload)


def emit_failure(reason: str, extra: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"ok": False, "reason": reason}
    if extra:
        payload.update(extra)
    json_stdout(payload)
