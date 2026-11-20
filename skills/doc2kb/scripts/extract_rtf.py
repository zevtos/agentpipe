#!/usr/bin/env python3
"""
extract_rtf.py — Phase 4 extractor for Rich Text Format (.rtf) files.

CLI:
    extract_rtf.py <input_rtf> <output_md>
                   [--doc-id doc-NNN]
                   [--source-rel relative/path/in/corpus.rtf]
                   [--assets-dir <abs_path>]
                   [--assets-rel <rel_prefix_from_md>]
                   [--no-extract-images]
                   [--force-striprtf]

Effect:
    Writes <output_md> with YAML frontmatter + Markdown body. Stdout receives
    a single JSON line summarizing the result.

Routing:
    - If `pandoc` is on PATH (and `--force-striprtf` is not set), the body is
      produced by pandoc (`-f rtf -t markdown`). Pandoc preserves headings,
      bold/italic, lists, tables, and embedded images (extracted to
      `<kb_dir>/assets/` via the shared `pandoc_to_markdown` helper).
    - Otherwise the body is produced by `striprtf` — a pure-Python, dependency
      -free RTF→plain-text converter from the lightweight tier. It always
      works (no system binary), but flattens structure and drops images, so a
      warning recommends installing pandoc for richer output.
    - `--force-striprtf` pins the pure-Python route even when pandoc exists
      (regression-test / no-subprocess escape hatch).

RTF is plain ASCII markup with `\\'xx` codepage escapes and `\\uN` unicode
escapes; striprtf decodes both. We therefore read the source as latin-1 (a
lossless byte→codepoint map) so the raw bytes reach striprtf intact and it can
apply the document's declared `\\ansicpg` codepage itself.

Codepages: pandoc's RTF reader decodes `\\'xx` only as cp1252 (plus the
`\\mac`/`\\pc`/`\\pca` charsets) and ignores `\\fcharset` — a Russian Word RTF
(`\\ansicpg1251`, fonts `\\fcharset204`) came out as `Ïðèëîæåíèå`. Before
pandoc we therefore rewrite every `\\'xx` run into `\\uN` escapes decoded with
the codepage in effect (the current font's charset, else the document's
`\\ansicpg`); `\\uN` fallback bytes, `\\bin` data and symbol fonts stay as they
are. If pandoc still reports an unsupported code page, the body would be
garbled, so the striprtf route is used instead.
"""
from __future__ import annotations

import argparse
import codecs
import re
import sys
import tempfile
from pathlib import Path

from _common import (  # noqa: E402
    add_assets_args,
    clean_whitespace,
    count_tokens,
    emit_failure,
    emit_success,
    pandoc_available,
    pandoc_to_markdown,
    resolve_assets_dir,
    sanitize_heading,
    sha256_of,
    today_iso,
    tool_version_string,
    validate_source_rel,
    write_md,
)


EXTRACTOR_PANDOC = "pandoc"
EXTRACTOR_STRIPRTF = "striprtf"
# Below this body length on a non-empty rtf, flag a warning.
MIN_BODY_CHARS = 50

# \fcharsetN → codec. 0 (ANSI) and 1 (default) follow the document's \ansicpg,
# as Word and striprtf read them; 2 (symbol) and unknown values have no text
# codec and keep their \'xx escapes.
_CHARSET_CODEC = {
    77: "mac_roman", 128: "cp932", 129: "cp949", 130: "johab", 134: "gbk",
    136: "cp950", 161: "cp1253", 162: "cp1254", 163: "cp1258", 177: "cp1255",
    178: "cp1256", 186: "cp1257", 204: "cp1251", 222: "cp874", 238: "cp1250",
    254: "cp437",
}
_DOC_CHARSETS = (0, 1)
_RTF_TOKEN = re.compile(
    r"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?"   # control word (+ its delimiter space)
    r"|\\'([0-9a-fA-F]{2})"                # hex escape
    r"|\\[\s\S]"                           # control symbol
    r"|[{}]"
    r"|[^\\{}]+")
_PANDOC_UNSUPPORTED_CP = "Unsupported code page"


def _codec(name: str | None) -> str | None:
    if not name:
        return None
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def _font_charsets(rtf: str) -> dict[int, int]:
    """{font number: \\fcharset} from the {\\fonttbl …} group."""
    start = rtf.find("{\\fonttbl")
    if start < 0:
        return {}
    depth, end, i = 0, len(rtf), start
    while i < len(rtf):
        c = rtf[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
        i += 1
    return {int(f): int(cs) for f, cs in
            re.findall(r"\\f(\d+)[^{}]*?\\fcharset(\d+)", rtf[start:end])}


def transcode_rtf_codepages(rtf: str) -> tuple[str, int]:
    """Rewrite codepage `\\'xx` escapes as `\\uN` so a cp1252-only reader
    (pandoc) gets the right characters. `rtf` is the file read as latin-1.
    Returns (rtf, characters converted); unchanged when nothing needs it."""
    if "\\'" not in rtf:
        return rtf, 0
    m = re.search(r"\\ansicpg(\d+)", rtf)
    doc_codec = _codec(f"cp{m.group(1)}" if m else "cp1252")
    fonts = _font_charsets(rtf)

    def font_codec(font: int | None) -> str | None:
        cs = fonts.get(font) if font is not None else None
        if cs is None or cs in _DOC_CHARSETS:
            return doc_codec
        return _codec(_CHARSET_CODEC.get(cs))

    if all(font_codec(f) in (None, "cp1252") for f in [None, *fonts]):
        return rtf, 0

    m = re.search(r"\\deff(\d+)", rtf)
    out: list[str] = []
    stack: list[tuple[int | None, int]] = []
    font, uc = (int(m.group(1)) if m else None), 1
    skip = 0                         # \uN fallback characters still to pass through
    run, run_codec = bytearray(), None
    converted = 0

    def flush() -> None:
        nonlocal run, converted
        if not run:
            return
        text = codecs.decode(bytes(run), run_codec, errors="replace")
        # Fallback as \'3f, not a literal "?": pandoc 3.12 skips one character
        # too many after a plain-text fallback (`ႂ? x` → "кx").
        fallback = "\\'3f" * uc if uc else " "
        for ch in text:
            units = ch.encode("utf-16-le")
            for k in range(0, len(units), 2):
                n = int.from_bytes(units[k:k + 2], "little")
                out.append(f"\\u{n - 65536 if n > 32767 else n}{fallback}")
        converted += len(text)
        run = bytearray()

    pos = 0
    while pos < len(rtf):
        tok = _RTF_TOKEN.match(rtf, pos)
        pos = tok.end()
        word, num, hexd = tok.group(1), tok.group(2), tok.group(3)
        if hexd is not None and skip == 0:
            codec = font_codec(font)
            if codec is not None and codec != "cp1252":
                if run and run_codec != codec:
                    flush()
                run_codec = codec
                run.append(int(hexd, 16))
                continue
        flush()
        out.append(tok.group(0))
        if hexd is not None:
            skip = max(0, skip - 1)
        elif word is not None:
            if word == "bin" and num:
                n = max(0, int(num))
                out.append(rtf[pos:pos + n])
                pos += n
            elif word == "f" and num:
                font = int(num)
            elif word == "uc" and num:
                uc = max(0, int(num))
            elif word == "u" and num:
                skip = uc
        elif tok.group(0) == "{":
            stack.append((font, uc))
            skip = 0
        elif tok.group(0) == "}":
            if stack:
                font, uc = stack.pop()
            skip = 0
        elif tok.group(0).startswith("\\"):
            skip = max(0, skip - 1)
        elif skip:
            text = tok.group(0)
            i = 0
            while skip and i < len(text):
                if text[i] not in "\r\n":
                    skip -= 1
                i += 1
    flush()
    if not converted:
        return rtf, 0
    return re.sub(r"\\ansicpg\d+", r"\\ansicpg1252", "".join(out), count=1), converted


def _extract_via_striprtf(input_rtf: Path) -> tuple[str, list[str]]:
    """Pure-Python fallback. Returns (body, warnings). Loses tables/images
    and most structure — striprtf emits plain text — but never needs a
    system binary, so the lightweight tier always has a working RTF path."""
    warnings: list[str] = []
    try:
        from striprtf.striprtf import rtf_to_text  # type: ignore
    except Exception as e:
        raise RuntimeError(f"striprtf unavailable: {e}") from e
    # latin-1 is a 1:1 byte→codepoint map: no decode error, raw bytes survive
    # so striprtf can apply the document's own \ansicpg / \'xx escapes.
    raw = input_rtf.read_text(encoding="latin-1")
    try:
        text = rtf_to_text(raw, errors="ignore")
    except Exception as e:
        raise RuntimeError(f"striprtf conversion failed: {e}") from e
    return clean_whitespace(text), warnings


def extract(input_rtf: Path,
            doc_id: str = "doc-000",
            assets_dir: Path | None = None,
            assets_rel_prefix: str = "../assets",
            extract_images: bool = True,
            force_striprtf: bool = False) -> tuple[str, dict, list[str], str]:
    """Returns (markdown_body, frontmatter_extras, warnings, extractor_name).

    Prefers pandoc (structure + images) and falls back to striprtf
    (plain text, always available). See the module docstring for routing."""
    warnings: list[str] = []
    assets: list[str] = []
    use_pandoc = pandoc_available() and not force_striprtf

    if use_pandoc:
        try:
            rtf, converted = transcode_rtf_codepages(input_rtf.read_text(encoding="latin-1"))
            with tempfile.TemporaryDirectory(prefix="doc2kb-rtf-") as tmp:
                pandoc_in = input_rtf
                if converted:
                    pandoc_in = Path(tmp) / "source.rtf"
                    pandoc_in.write_text(rtf, encoding="latin-1")
                body, w, a = pandoc_to_markdown(
                    pandoc_in,
                    from_format="rtf",
                    doc_id=doc_id,
                    assets_dir=assets_dir,
                    assets_rel_prefix=assets_rel_prefix,
                    extract_images=extract_images,
                )
            garbled = [x for x in w if _PANDOC_UNSUPPORTED_CP in x]
            if garbled:
                raise RuntimeError(garbled[0].removeprefix("pandoc: ").strip())
            warnings.extend(w)
            assets.extend(a)
            extractor_name = EXTRACTOR_PANDOC
        except RuntimeError as e:
            # Pandoc failure → fall back to striprtf with a loud warning so
            # the operator knows structure/images were lost.
            warnings.append(
                f"pandoc route failed ({e}); falling back to striprtf — "
                "tables, images and most structure will be flattened to text"
            )
            body, w = _extract_via_striprtf(input_rtf)
            warnings.extend(w)
            extractor_name = EXTRACTOR_STRIPRTF
    else:
        body, w = _extract_via_striprtf(input_rtf)
        warnings.extend(w)
        extractor_name = EXTRACTOR_STRIPRTF
        if not force_striprtf:
            warnings.append(
                "pandoc not on PATH — extracted via striprtf (plain text). "
                "Install pandoc (`brew install pandoc` / `apt install pandoc`) "
                "and re-run to preserve tables, images, and headings"
            )

    if len(body.strip()) < MIN_BODY_CHARS:
        warnings.append(f"extracted body is unusually short ({len(body)} chars)")

    headings = []
    for line in body.split("\n"):
        if line.startswith("# ") and len(line) < 200:
            sanitized = sanitize_heading(line[2:])
            if sanitized:
                headings.append(sanitized)
            if len(headings) >= 10:
                break
    extras: dict = {"headings": headings}
    if assets:
        extras["assets"] = assets
    return body, extras, warnings, extractor_name


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extract RTF → Markdown. Routes through pandoc when "
                    "available; otherwise via the pure-Python striprtf fallback."
    )
    ap.add_argument("input_rtf")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    add_assets_args(ap)
    ap.add_argument("--force-striprtf", action="store_true",
                    help="Bypass the pandoc route even when pandoc is on PATH.")
    args = ap.parse_args()

    in_path = Path(args.input_rtf).expanduser().resolve()
    out_path = Path(args.output_md).expanduser().resolve()
    source_rel = args.source_rel or in_path.name
    try:
        source_rel = validate_source_rel(source_rel)
    except ValueError as e:
        emit_failure(f"invalid --source-rel: {e}")
        return 1

    if not in_path.is_file():
        emit_failure(f"input not found: {in_path}")
        return 1

    assets_dir: Path | None = resolve_assets_dir(args, out_path)

    try:
        body, extras, warnings, extractor_name = extract(
            in_path,
            doc_id=args.doc_id,
            assets_dir=assets_dir,
            assets_rel_prefix=args.assets_rel,
            extract_images=not args.no_extract_images,
            force_striprtf=args.force_striprtf,
        )
    except Exception as e:
        emit_failure(f"extraction failed: {e}", extra={"input": str(in_path)})
        return 1

    fm = {
        "id": args.doc_id,
        "source": source_rel,
        "source_type": "rtf",
        "source_sha256": sha256_of(in_path),
        "extraction_method": f"{extractor_name}@{tool_version_string()}",
        "extraction_date": today_iso(),
        "headings": extras.get("headings", []),
        "tokens_estimated": count_tokens(body),
        "warnings": warnings,
    }
    if extras.get("assets"):
        fm["assets"] = extras["assets"]
    write_md(out_path, fm, body)
    emit_success(out_path, body, warnings, extra={
        "extractor": extractor_name,
        "assets_extracted": len(extras.get("assets", [])),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
