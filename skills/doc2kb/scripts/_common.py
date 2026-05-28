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

# Image-validator constants for save_image_safe(). Claude's vision encoder
# rejects/garbles images where max(width, height) > ~2000 px — the model
# returns a generic "image too large" response instead of analysing the
# content. We downscale anything larger to stay inside the working range.
# Min dimension filters obvious decorative pixels (1×1 spacers, 4×4 bullet
# glyphs) that otherwise clutter the kb assets dir without adding signal.
MAX_IMAGE_DIMENSION = 2000
MIN_IMAGE_DIMENSION = 16
# Default JPEG quality for downscaled photos. Re-encoding at 90 keeps file
# size manageable while staying visually lossless to the vision model.
DEFAULT_JPEG_QUALITY = 90


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


def nfc_optional(s: str | None) -> str | None:
    """`nfc()` extended to tolerate None. Used by build_manifest when joining
    documents to scout entries by `source_path`, where the field is optional
    in the manifest schema. Returns None unchanged."""
    return nfc(s) if s is not None else None


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


# ---------- ligature recovery ----------

# pymupdf4llm (≤1.27.x) drops one letter from PDF ligature glyphs in its
# spans→markdown pipeline: PDFs whose ToUnicode CMap maps the "fi"/"ff"/"fl"
# glyph to a single ASCII char come out with the second letter missing
# ("Official" → "Ofcial", "flexible" → "fexible", "traffic" → "trafc").
# Raw `pymupdf.Page.get_text("dict")` returns the words correctly — the bug
# is in pymupdf4llm's higher-level reassembly — so we patch it on our side
# with a hard-coded word-prefix list. Patterns match at word boundaries
# only; replacements preserve the original case of the first character so
# "Ofcial" → "Official" but "ofcial" → "official".
#
# The list covers the broken ligatures observed in real PDF corpora
# (academic papers, English-language reference books, lab manuals). Adding
# a new pattern is safe as long as: (a) the broken prefix does not appear
# as a legitimate English word, and (b) the fix only modifies the prefix —
# the rest of the word (suffix capture group, if any) survives intact.
# Each entry is (compiled regex, replacement-for-matched-prefix). The
# regex matches a *prefix* of a broken word at a soft word boundary;
# any trailing letters (suffixes like `ly`, `ity`, `s`, `ed`, `ing`, …)
# survive untouched because they sit outside the match. We use a
# negative-lookbehind `(?<![A-Za-z])` instead of `\b` so the pattern
# also fires when the broken word is wrapped in markdown italic
# (`_fnd_` → `_find_`); `\b` would fail because `_` is a word char.
# Lookaheads disambiguate prefixes that double as legitimate English
# (`dif` → legit when followed by `f`; broken only when followed by
# `cult` / `eren`).
_LB = r"(?<![A-Za-z])"
_LIGATURE_FIXES: list[tuple[re.Pattern[str], str]] = [
    # fi-drop: middle `i` missing from the original `fi` ligature
    (re.compile(_LB + r"fexib",           re.IGNORECASE), "flexib"),
    (re.compile(_LB + r"ofcial",          re.IGNORECASE), "official"),
    (re.compile(_LB + r"specifc",         re.IGNORECASE), "specific"),
    (re.compile(_LB + r"signifc",         re.IGNORECASE), "signific"),
    (re.compile(_LB + r"infnit",          re.IGNORECASE), "infinit"),
    (re.compile(_LB + r"fnit(?=e|es)",    re.IGNORECASE), "finit"),
    (re.compile(_LB + r"fgure",           re.IGNORECASE), "figure"),
    (re.compile(_LB + r"fnal",            re.IGNORECASE), "final"),
    (re.compile(_LB + r"fnish",           re.IGNORECASE), "finish"),
    (re.compile(_LB + r"fnding",          re.IGNORECASE), "finding"),
    (re.compile(_LB + r"frst",            re.IGNORECASE), "first"),
    (re.compile(_LB + r"confrm",          re.IGNORECASE), "confirm"),
    (re.compile(_LB + r"defn(?=[a-z])",   re.IGNORECASE), "defin"),
    (re.compile(_LB + r"fnd(?![A-Za-z])", re.IGNORECASE), "find"),
    (re.compile(_LB + r"fnd(?=s|ing)",    re.IGNORECASE), "find"),
    (re.compile(_LB + r"fts(?![A-Za-z])", re.IGNORECASE), "fits"),
    (re.compile(_LB + r"fle(?=s?(?![A-Za-z])|d(?![A-Za-z]))", re.IGNORECASE), "file"),
    (re.compile(_LB + r"fxed",            re.IGNORECASE), "fixed"),
    (re.compile(_LB + r"fve(?![A-Za-z])", re.IGNORECASE), "five"),
    (re.compile(_LB + r"ftt(?=ing|ed)",   re.IGNORECASE), "fitt"),
    (re.compile(_LB + r"beneft",          re.IGNORECASE), "benefit"),
    (re.compile(_LB + r"clarifc",         re.IGNORECASE), "clarific"),
    # ff-drop: one of two `f`s missing
    (re.compile(_LB + r"efect",           re.IGNORECASE), "effect"),
    (re.compile(_LB + r"efort",           re.IGNORECASE), "effort"),
    (re.compile(_LB + r"afect",           re.IGNORECASE), "affect"),
    (re.compile(_LB + r"aford",           re.IGNORECASE), "afford"),
    # `dif` (3 chars, broken from `diff` where one `f` was dropped) — the
    # `er` lookahead is safe for legit `different`/`differ`/`difference`
    # because they have `diff` (4 chars), so position 3 is `f`, not `e`,
    # and the lookahead fails for them.
    (re.compile(_LB + r"dif(?=cult|er)",  re.IGNORECASE), "diff"),
    # ffi-drop: `i` dropped from `ffi`. Only `cul` lookahead (`difficult`
    # → broken `diffcul`); `eren` lookahead would over-correct legit
    # `different` → `diffierent`.
    (re.compile(_LB + r"diff(?=cul)",     re.IGNORECASE), "diffi"),
    # Agentive `-fier` nouns where the `fi` ligature dropped its `i`:
    # `quantifier` → `quantifer`, `modifier` → `modifer`. Hardcoded list
    # because a general `\Bfer\b` pattern would clobber legit `defer`,
    # `prefer`, `refer`, `transfer`, `confer`, `infer`, etc.
    (re.compile(_LB + r"quantifer",       re.IGNORECASE), "quantifier"),
    (re.compile(_LB + r"modifer",         re.IGNORECASE), "modifier"),
    (re.compile(_LB + r"identifer",       re.IGNORECASE), "identifier"),
    (re.compile(_LB + r"classifer",       re.IGNORECASE), "classifier"),
    (re.compile(_LB + r"specifer",        re.IGNORECASE), "specifier"),
    (re.compile(_LB + r"amplifer",        re.IGNORECASE), "amplifier"),
    (re.compile(_LB + r"verifer",         re.IGNORECASE), "verifier"),
    (re.compile(_LB + r"eficien",         re.IGNORECASE), "efficien"),
    (re.compile(_LB + r"efcien",          re.IGNORECASE), "efficien"),
    (re.compile(_LB + r"trafc",           re.IGNORECASE), "traffic"),
    (re.compile(_LB + r"sufcient",        re.IGNORECASE), "sufficient"),
    (re.compile(_LB + r"cofcient",        re.IGNORECASE), "coefficient"),
]


# Residual-broken-ligature detector: matches any word with `f` followed
# by a non-l/r/aeiou consonant. We filter out the legitimate English
# `<vowel>ft` cluster (aft, ift, oft, uft — gift, lift, shift, draft, …)
# and `lf`/`rf` endings via a negative lookbehind so the warning only
# fires on truly suspicious patterns. When the warning triggers,
# operator should extend _LIGATURE_FIXES with the new broken word(s).
_RESIDUAL_LIGATURE_RE = re.compile(
    r"\b[a-z]*(?<![aiou])f[bcdgkmnpqsvwxz][a-z]+\b",
    re.IGNORECASE,
)


def _preserve_first_case(matched: str, replacement: str) -> str:
    """If the matched text starts uppercase, capitalize the replacement."""
    if matched and matched[0].isupper() and replacement and replacement[0].islower():
        return replacement[0].upper() + replacement[1:]
    return replacement


def recover_ligatures(text: str) -> tuple[str, int]:
    """Repair pymupdf4llm's dropped-ligature words via known patterns.

    Returns (new_text, total_replacements). Idempotent: re-running on
    already-fixed text yields zero replacements. Case of the first
    character is preserved (`Ofcial`→`Official`, `ofcial`→`official`)."""
    total = 0
    for pattern, replacement in _LIGATURE_FIXES:
        def _sub(m: re.Match, _repl: str = replacement) -> str:
            new = m.expand(_repl)
            return _preserve_first_case(m.group(0), new)
        text, n = pattern.subn(_sub, text)
        total += n
    return text, total


# ---------- page footer cleanup ----------

# Footer page numbers in PDFs typically appear as a standalone integer line
# right before the next `[page N]` anchor (the number itself is the page
# being closed, the anchor introduces the next page). detect_recurring_lines
# in normalize_md cannot catch them because each footer line is unique
# (1, 2, 3, …). This regex grabs the standalone number that sits *between*
# two consecutive page anchors with only whitespace around it, plus any
# stray number at the very end of the body (last-page footer).
_PAGE_FOOTER_BETWEEN_RE = re.compile(
    r"\n\n\d{1,4}\n+(?=\[page \d+\])",
)
_PAGE_FOOTER_TAIL_RE = re.compile(
    r"\n\n\d{1,4}\n*\Z",
)


def strip_page_footer_numbers(body: str) -> tuple[str, int]:
    """Remove standalone footer page numbers that sit between two
    `[page N]` anchors (or at the document tail). Preserves the anchors
    themselves — they're used by the second-session agent for navigation.

    Returns (new_body, removed_count)."""
    removed = 0
    body, n1 = _PAGE_FOOTER_BETWEEN_RE.subn("\n\n", body)
    removed += n1
    body, n2 = _PAGE_FOOTER_TAIL_RE.subn("\n", body)
    removed += n2
    return body, removed


# ---------- file writer ----------

def write_md(out_path: Path, frontmatter: dict[str, Any], body: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fm = render_frontmatter(frontmatter)
    body = clean_whitespace(body)
    out_path.write_text(fm + body, encoding="utf-8")


# ---------- page list / page-section helpers ----------

# Shared by mineru's --pages flag and any future page-targeted extractor.
# Keeping them in _common.py so the splice logic stays in one place even if
# more callers grow it.

PAGE_ANCHOR_RE = re.compile(r"^\[page (\d+)\]\s*$", re.MULTILINE)


def parse_page_list(spec: str) -> list[int]:
    """Parse a comma-separated page spec with optional `start-end` ranges
    into a sorted, deduplicated list of 1-based page numbers.

    Examples:
        "2,18-19,35,221,243-244" → [2, 18, 19, 35, 221, 243, 244]
        "10" → [10]
        " 5 , 7-9 " → [5, 7, 8, 9]

    Raises ValueError on malformed input (non-numeric tokens, descending
    ranges, zero/negative pages) so the caller can surface the exact
    problem to the user.
    """
    if not spec or not spec.strip():
        raise ValueError("page list is empty")
    pages: set[int] = set()
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        if "-" in token:
            lo_s, _, hi_s = token.partition("-")
            try:
                lo = int(lo_s.strip())
                hi = int(hi_s.strip())
            except ValueError as e:
                raise ValueError(
                    f"invalid range {token!r}: expected `int-int`"
                ) from e
            if lo < 1 or hi < 1:
                raise ValueError(
                    f"page numbers must be ≥ 1 (got {token!r})"
                )
            if hi < lo:
                raise ValueError(
                    f"range {token!r} is descending (got {lo}-{hi})"
                )
            pages.update(range(lo, hi + 1))
        else:
            try:
                p = int(token)
            except ValueError as e:
                raise ValueError(
                    f"invalid page {token!r}: expected integer or range"
                ) from e
            if p < 1:
                raise ValueError(f"page numbers must be ≥ 1 (got {p})")
            pages.add(p)
    if not pages:
        raise ValueError("page list contained no usable entries")
    return sorted(pages)


def format_page_list(pages: Iterable[int]) -> str:
    """Render a list of page numbers as a compact, range-collapsed string.
    Inverse of parse_page_list when feasible.

    [2, 3, 4, 7, 10, 11] → "2-4, 7, 10-11"
    """
    sorted_pages = sorted(set(int(p) for p in pages))
    if not sorted_pages:
        return ""
    out: list[str] = []
    start = prev = sorted_pages[0]
    for p in sorted_pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = p
    out.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(out)


def split_body_by_page_anchors(body: str) -> tuple[str, dict[int, str]]:
    """Split a `[page N]`-anchored markdown body into a preamble and a dict
    of `{page_no: section}` entries. Each section starts at its `[page N]`
    anchor line and ends just before the next anchor (or at end-of-body).

    Used by mineru's `--patch-into` splice mode to replace specific page
    sections in an existing target file. Order is preserved by sorted page
    numbers when callers rebuild the body.

    The preamble (anything before the first `[page N]`) is returned as the
    first element so it round-trips intact — markdown rendered before the
    first page anchor often contains the cover title, TOC, or YAML-style
    front-page header.
    """
    matches = list(PAGE_ANCHOR_RE.finditer(body))
    if not matches:
        return body, {}
    preamble = body[: matches[0].start()].rstrip("\n")
    sections: dict[int, str] = {}
    for i, m in enumerate(matches):
        page_no = int(m.group(1))
        section_start = m.start()
        section_end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[page_no] = body[section_start:section_end].rstrip()
    return preamble, sections


def assemble_body_from_sections(
    preamble: str,
    sections: dict[int, str],
) -> str:
    """Inverse of split_body_by_page_anchors. Sections are emitted in
    ascending page-number order with two blank lines between them. The
    preamble (if non-empty) is prepended with two blank lines before the
    first page section.
    """
    parts: list[str] = []
    if preamble.strip():
        parts.append(preamble.rstrip() + "\n")
    for page_no in sorted(sections):
        parts.append(sections[page_no].rstrip() + "\n")
    return "\n".join(parts).rstrip() + "\n"


# ---------- frontmatter reader ----------

FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_FRONTMATTER_RE = FRONTMATTER_RE  # internal alias preserved for old callers

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


# ---------- image validator + safe writer ----------

# Vector / metafile formats the vision encoder cannot render. Emit a warning
# and skip — re-encoding them as raster would require a rasterizer
# (e.g. CairoSVG, Inkscape) outside the lightweight tier.
_NON_VIEWABLE_EXTENSIONS = {"wmf", "emf", "svg", "pdf", "eps"}


class ImageSaveResult:
    """Outcome of save_image_safe(). Use bool(result) to test success.

    Attributes:
        path: Path the bytes were written to, or None if skipped.
        reason: Short human-readable status ('saved', 'downscaled',
                'skipped_too_small', 'skipped_format', 'skipped_corrupt',
                'skipped_duplicate', 'passthrough').
        width, height: Final raster dimensions, if known.
        original_width, original_height: Pre-resize dimensions, if known.
        downscaled: True iff the image was reduced to fit MAX_IMAGE_DIMENSION.
        sha256: First-16-hex of content hash, used for dedup across calls.
    """

    __slots__ = (
        "path", "reason", "width", "height",
        "original_width", "original_height",
        "downscaled", "sha256",
    )

    def __init__(self, path: Path | None, reason: str,
                 width: int | None = None, height: int | None = None,
                 original_width: int | None = None,
                 original_height: int | None = None,
                 downscaled: bool = False, sha256: str | None = None):
        self.path = path
        self.reason = reason
        self.width = width
        self.height = height
        self.original_width = original_width
        self.original_height = original_height
        self.downscaled = downscaled
        self.sha256 = sha256

    def __bool__(self) -> bool:
        return self.path is not None


def _image_content_hash(image_bytes: bytes) -> str:
    """Short SHA-256 prefix used by save_image_safe() for in-document dedup."""
    return hashlib.sha256(image_bytes).hexdigest()[:16]


def save_image_safe(
    image_bytes: bytes,
    target_path: Path,
    *,
    max_dim: int = MAX_IMAGE_DIMENSION,
    min_dim: int = MIN_IMAGE_DIMENSION,
    dedup_cache: dict[str, Path] | None = None,
    source_ext: str | None = None,
) -> ImageSaveResult:
    """Validate, optionally downscale, and write an image extracted from a
    source document. The single chokepoint every extractor must call instead
    of `path.write_bytes(...)` so the kb never contains an image larger than
    MAX_IMAGE_DIMENSION on its longer side (Claude's vision encoder breaks
    on those) and never contains 1×1 spacer pixels (decorative noise).

    Behaviour:
      - min(width, height) < min_dim → skipped (likely spacer/decoration).
      - max(width, height) > max_dim → resized with LANCZOS so that
        max(width, height) == max_dim, aspect ratio preserved.
      - Animated GIFs / multi-frame TIFFs → first frame only.
      - Palette/RGBA images encoded as JPEG → converted to RGB first.
      - Unsupported formats (WMF/EMF/SVG/PDF/EPS) → skipped (vision encoder
        cannot render vector). Caller is expected to surface a warning.
      - Corrupt / undecodable bytes → skipped.
      - Identical bytes saved already this run (per dedup_cache) → returns
        the existing path with reason='skipped_duplicate'. dedup is opt-in
        so callers can disable when filenames must be unique per location.

    The output extension may differ from `target_path.suffix` when Pillow
    coerces a JPEG/palette combination — check `result.path` for the actual
    location.

    Returns ImageSaveResult. Callers should test truthiness (`if result:`)
    rather than `result.path is not None` for readability.
    """
    if not image_bytes:
        return ImageSaveResult(None, "skipped_empty")

    digest = _image_content_hash(image_bytes)
    if dedup_cache is not None and digest in dedup_cache:
        return ImageSaveResult(
            dedup_cache[digest], "skipped_duplicate", sha256=digest,
        )

    ext_lc = (source_ext or target_path.suffix.lstrip(".")).lower()
    if ext_lc in _NON_VIEWABLE_EXTENSIONS:
        return ImageSaveResult(None, "skipped_format", sha256=digest)

    try:
        from PIL import Image, ImageFile  # type: ignore
        # Be tolerant of truncated streams (common in office docs that
        # embed PNG/JPEG slightly past the IEND/EOI marker). LOAD_TRUNCATED
        # lets us recover what's there instead of dropping the image.
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        from io import BytesIO  # type: ignore
    except ImportError:
        # Pillow is part of the lightweight tier; if it's missing the venv
        # is broken. Pass bytes through unchanged so we don't silently lose
        # data, but flag with a 'passthrough' reason so caller can warn.
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(image_bytes)
        if dedup_cache is not None:
            dedup_cache[digest] = target_path
        return ImageSaveResult(target_path, "passthrough", sha256=digest)

    try:
        img = Image.open(BytesIO(image_bytes))
        img.load()  # force decode so corrupt streams fail here, not later
    except Exception:
        return ImageSaveResult(None, "skipped_corrupt", sha256=digest)

    original_w, original_h = img.size
    if min(original_w, original_h) < min_dim:
        return ImageSaveResult(
            None, "skipped_too_small",
            original_width=original_w, original_height=original_h,
            sha256=digest,
        )

    w, h = original_w, original_h
    downscaled = False
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        try:
            img = img.resize((new_w, new_h), Image.LANCZOS)
        except Exception:
            try:
                img = img.convert("RGB").resize((new_w, new_h), Image.LANCZOS)
            except Exception:
                return ImageSaveResult(
                    None, "skipped_corrupt",
                    original_width=original_w, original_height=original_h,
                    sha256=digest,
                )
        w, h = new_w, new_h
        downscaled = True

    fmt = (img.format or "").upper()
    if not fmt:
        # No format on first decode — fall back to extension or PNG.
        fmt = (target_path.suffix.lstrip(".") or "PNG").upper()
    if fmt == "JPG":
        fmt = "JPEG"
    # JPEG cannot store alpha — flatten to RGB.
    if fmt == "JPEG" and img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")

    # Match the saved extension to the actual format (some Office files
    # mislabel a PNG as `.bin` etc.). Use the canonical lowercase mapping.
    fmt_to_ext = {
        "JPEG": "jpeg", "PNG": "png", "GIF": "gif",
        "BMP": "bmp", "TIFF": "tiff", "WEBP": "webp",
    }
    target_ext = fmt_to_ext.get(fmt, target_path.suffix.lstrip(".").lower() or "png")
    if target_path.suffix.lstrip(".").lower() != target_ext:
        target_path = target_path.with_suffix(f".{target_ext}")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    save_kwargs: dict[str, Any] = {"format": fmt}
    if fmt == "JPEG":
        save_kwargs["quality"] = DEFAULT_JPEG_QUALITY
        save_kwargs["optimize"] = True
    elif fmt == "PNG":
        save_kwargs["optimize"] = True

    try:
        img.save(target_path, **save_kwargs)
    except Exception:
        # Last-ditch: try PNG with explicit RGB conversion.
        try:
            target_path = target_path.with_suffix(".png")
            img.convert("RGB").save(target_path, format="PNG", optimize=True)
            fmt = "PNG"
        except Exception:
            return ImageSaveResult(
                None, "skipped_corrupt",
                width=w, height=h,
                original_width=original_w, original_height=original_h,
                sha256=digest,
            )

    if dedup_cache is not None:
        dedup_cache[digest] = target_path
    return ImageSaveResult(
        target_path,
        "downscaled" if downscaled else "saved",
        width=w, height=h,
        original_width=original_w, original_height=original_h,
        downscaled=downscaled, sha256=digest,
    )
