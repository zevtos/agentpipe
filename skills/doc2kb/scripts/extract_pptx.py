#!/usr/bin/env python3
"""
extract_pptx.py — Phase 4 extractor for PPTX files. Critically, this
preserves speaker notes — they often contain more semantic content than the
slides themselves, and most off-the-shelf converters drop them. Equally
critical, this extracts embedded pictures to disk via save_image_safe() —
without that, diagram-only slides ("Пример 1", "Пример 2", …) collapse to
just the header and the second-session agent has no way to know what the
slide was about.

CLI:
    extract_pptx.py <input_pptx> <output_md>
                    [--doc-id doc-NNN]
                    [--source-rel relative/path/in/corpus.pptx]
                    [--assets-dir <abs_path>]
                    [--assets-rel <rel_prefix_from_md>]
                    [--no-extract-images]

Output layout (per slide):

    ## Slide N: <best-effort title>

    <body text from text frames, in shape order>

    ![Slide N picture M](../assets/doc-NNN-slideNN-imgM.png)

    | header | cells |
    | ---    | ---   |
    | ...    | ...   |

    ### Notes

    <speaker notes>

    ---  (slide separator)

The body intentionally interleaves shape contents in document order rather
than re-ordering by position — readers care about semantics, not layout.

Image handling:
    - By default, embedded PICTURE shapes (at any group depth) are extracted
      via save_image_safe() to the kb's `assets/` directory and referenced
      from the slide body as Markdown image links. Animations,
      palette/RGBA-as-JPEG mismatches, and tiny decorative pixels are
      filtered automatically.
    - WMF/EMF (Visio paste, "vector clipart") are rasterized to PNG through
      LibreOffice (one batch per deck); only when `soffice` is missing or
      fails are they skipped, with the reason in `warnings`.
    - `--no-extract-images` reverts to the old count-only behaviour.

Objects:
    - Charts render as a title line plus a category × series table built
      from the values cached in the chart XML.
    - SmartArt diagrams render as `*(SmartArt)*` plus a bullet list of their
      node texts (python-pptx does not expose SmartArt at all).
    - OLE objects (an embedded Word file, workbook, PDF, …) show their
      preview image and a link to the embedded document, which is extracted
      as its own doc (`docs/<id>_eNN-*.md`, see `_embedded.py`); the
      chart-data workbook behind a chart is linked the same way.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from _embedded import EmbedSession  # noqa: E402
from _common import (  # noqa: E402
    add_assets_args,
    clean_whitespace,
    count_tokens,
    emit_failure,
    emit_success,
    resolve_assets_dir,
    sanitize_heading,
    save_image_safe,
    skipped_image_note,
    sha256_of,
    today_iso,
    tool_version_string,
    validate_source_rel,
    write_md,
)


EXTRACTOR_NAME = "python-pptx"


def _try_import():
    try:
        from pptx import Presentation  # type: ignore
        from pptx.enum.shapes import MSO_SHAPE_TYPE  # type: ignore
        return Presentation, MSO_SHAPE_TYPE
    except Exception as e:
        emit_failure(f"python-pptx unavailable: {e}")
        sys.exit(1)


def _is_picture_like(shape) -> bool:
    """True if `shape` carries an embedded raster the way a Picture shape
    does, even when its `shape_type` is PLACEHOLDER rather than PICTURE.
    `PlaceholderPicture` inherits from `Picture` in python-pptx — both
    expose `shape.image.blob`, but the placeholder is classified by
    placeholder type and would otherwise fall through the PICTURE branch
    silently. Catches the common "title + image" slide layout that
    BPMN/UML tutorial decks use to show one diagram per slide."""
    if shape is None:
        return False
    try:
        from pptx.shapes.picture import Picture  # type: ignore
    except Exception:
        Picture = None  # noqa: N806
    if Picture is not None and isinstance(shape, Picture):
        return True
    # Fallback duck-typing — some pptx variants expose `.image` on objects
    # that don't subclass Picture.
    img = getattr(shape, "image", None)
    return img is not None and hasattr(img, "blob")


# Placeholder types that contain layout chrome (slide number, header, footer,
# date). These are noise for LLM consumption — skip them.
# Values from pptx.enum.text.PP_PLACEHOLDER:
#   13 SLIDE_NUMBER, 14 HEADER, 15 FOOTER, 16 DATE
_SKIP_PLACEHOLDER_TYPES = {13, 14, 15, 16}


def _is_chrome_placeholder(shape) -> bool:
    try:
        if not shape.is_placeholder:
            return False
        return int(shape.placeholder_format.type) in _SKIP_PLACEHOLDER_TYPES
    except Exception:
        return False


_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
# mc:Choice branches we can read: a14 = Office 2010 drawing (a14:m equations,
# rendered through _omml). Any other requirement (ink, 3D models, chartex …)
# takes mc:Fallback, usually a picture of the same content.
_READABLE_REQUIRES = {"a14"}


def _unwrap_alternate_content(sp_tree) -> int:
    """Replace each shape-level `mc:AlternateContent` with the shapes of the
    branch we can read. python-pptx skips the wrapper entirely, so a text box
    holding an equation — PowerPoint always wraps those — vanished with all its
    text. Mutates the in-memory slide XML only. Returns the number unwrapped."""
    n = 0
    for ac in list(sp_tree.iter(f"{{{_MC_NS}}}AlternateContent")):
        parent = ac.getparent()
        if parent is None or _local(parent.tag) not in ("spTree", "grpSp"):
            continue  # graphicFrame-level wrappers (OLE) are read by _ole_info
        branch = next((c for c in ac if _local(c.tag) == "Choice" and c.get("Requires")
                       and set(c.get("Requires").split()) <= _READABLE_REQUIRES), None)
        if branch is None:
            branch = next((c for c in ac if _local(c.tag) == "Fallback"), None)
        at = parent.index(ac)
        for k, shape_el in enumerate(list(branch) if branch is not None else []):
            parent.insert(at + k, shape_el)
        parent.remove(ac)
        n += 1
    return n


def _para_text(para) -> str:
    """A paragraph's text in document order: runs and fields, a line break as
    "\\n" (joining `para.runs` glued the words on both sides of one), and
    equations (`a14:m` → OMML) as `$…$` LaTeX, `$$…$$` for a display
    equation — python-pptx leaves those out."""
    out: list[str] = []
    for child in para._p:
        tag = _local(child.tag)
        if tag in ("r", "fld"):
            out.append("".join(t.text or "" for t in child if _local(t.tag) == "t"))
        elif tag == "br":
            out.append("\n")
        elif tag not in ("pPr", "endParaRPr"):
            import _omml  # noqa: E402
            for m in _omml.math_elements(child) if tag != "oMathPara" else [child]:
                latex = _omml.to_latex(m)
                if latex:
                    out.append(f"$${latex}$$" if _local(m.tag) == "oMathPara" else f"${latex}$")
    return "".join(out)


def _tf_text(tf) -> str:
    return "\n".join(_para_text(p) for p in tf.paragraphs)


def _shape_text(shape) -> str:
    if not getattr(shape, "has_text_frame", False):
        return ""
    try:
        tf = shape.text_frame
    except Exception:
        return ""
    return _tf_text(tf).rstrip()


def _table_md(shape) -> str:
    """Renders a pptx table as a Markdown pipe-table. Cells are joined with
    a space when they contain multiple paragraphs to avoid breaking the
    pipe-table format."""
    table = shape.table
    rows = []
    for row in table.rows:
        cells = []
        for cell in row.cells:
            tf = cell.text_frame
            txt = " ".join(t for t in (_para_text(p) for p in tf.paragraphs) if t)
            txt = txt.replace("\n", " ").strip()
            # Markdown pipes inside cells break the table; escape them.
            txt = txt.replace("|", "\\|")
            cells.append(txt)
        rows.append(cells)
    if not rows:
        return ""
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    sep = ["---"] * ncols
    lines = ["| " + " | ".join(rows[0]) + " |",
             "| " + " | ".join(sep) + " |"]
    for r in rows[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _best_title(slide) -> str | None:
    # 1) explicit title placeholder
    for shape in slide.shapes:
        try:
            if shape.is_placeholder and shape.placeholder_format.idx == 0:
                t = _shape_text(shape).strip().split("\n", 1)[0]
                if t:
                    return t[:200]
        except Exception:
            pass
    # 2) first text shape's first line, if it looks like a heading
    for shape in slide.shapes:
        try:
            if shape.has_text_frame:
                t = _shape_text(shape).strip().split("\n", 1)[0]
                if t and len(t) < 200:
                    return t
        except Exception:
            pass
    return None


def _picture_caption(shape) -> str:
    """Pull a human-friendly caption from a PICTURE shape. Tries alt_text
    first (set when authors describe the image for accessibility), falls
    back to the shape name (often "Picture 12" — uninformative but stable),
    then to "" so the caller can decide what to write."""
    for attr in ("alt_text", "name"):
        try:
            val = getattr(shape, attr, "") or ""
            val = val.strip()
            if val and val.lower() not in ("picture", "image"):
                return sanitize_heading(val, maxlen=120)
        except Exception:
            pass
    return ""


def _save_image_blob(
    blob: bytes,
    ext: str,
    caption: str,
    doc_id: str,
    slide_idx: int,
    pic_idx: int,
    assets_dir: Path,
    assets_rel_prefix: str,
    dedup_cache: dict,
    warnings: list[str],
) -> tuple[str, str] | None:
    """Run one slide image through save_image_safe() (vector formats are
    rasterized there) and return (markdown_image_ref, asset_relpath), or None
    when it was filtered (too small, unrenderable) — the caller decides on a
    fallback marker.

    The filename pattern is `<doc_id>-slideNN-imgM.<ext>`. Deduplication is
    intra-document only: a logo repeated on every slide saves disk by
    reusing the first occurrence, but two different documents that share a
    bitwise-identical image still keep separate copies (cross-doc dedup
    would require a manifest-level pass).
    """
    filename = f"{doc_id}-slide{slide_idx:02d}-img{pic_idx}.{ext}"
    target = assets_dir / filename
    result = save_image_safe(
        blob, target, dedup_cache=dedup_cache, source_ext=ext,
    )
    if not result:
        note = skipped_image_note(result, ext)
        if note:
            warnings.append(f"slide {slide_idx} picture {pic_idx}: {note}")
        # too-small / duplicate-with-no-prior-path: silent, by design.
        return None
    rel = f"{assets_rel_prefix}/{result.path.name}"
    return f"![{caption}]({rel})", rel


def _save_picture_shape(shape, doc_id, slide_idx, pic_idx, assets_dir,
                        assets_rel_prefix, dedup_cache, warnings):
    try:
        image = shape.image  # type: ignore[attr-defined]
        blob = image.blob
        ext = (image.ext or "png").lower()
    except Exception as e:
        warnings.append(
            f"slide {slide_idx} picture {pic_idx}: cannot read image blob ({e})"
        )
        return None
    caption = _picture_caption(shape) or f"Slide {slide_idx} picture {pic_idx}"
    return _save_image_blob(blob, ext, caption, doc_id, slide_idx, pic_idx,
                            assets_dir, assets_rel_prefix, dedup_cache, warnings)


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


_R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"


def _ole_info(shape) -> dict | None:
    """For a graphic frame holding an OLE object: the embedded part name
    (None for a linked object), the preview image blob, ProgID and the
    show-as-icon flag. Reads the XML directly so placeholder frames and
    `mc:AlternateContent` wrappers are covered too. None if not OLE."""
    el = getattr(shape, "_element", None)
    if el is None:
        return None
    oles = [e for e in el.iter() if _local(e.tag) == "oleObj"]
    if not oles:
        return None
    ole = next((o for o in oles if o.get(_R_ID)), oles[0])
    info = {"member": None, "preview": None, "prog_id": ole.get("progId") or "",
            "icon": (ole.get("showAsIcon") or "").lower() in ("1", "true")}
    part = getattr(shape, "part", None)
    rid = ole.get(_R_ID)
    if rid and part is not None:
        try:
            info["member"] = str(part.related_part(rid).partname).lstrip("/")
        except Exception:  # noqa: BLE001 — dangling rel: treat as linked
            pass
    for e in el.iter():
        if _local(e.tag) == "blip" and e.get(_R_EMBED) and part is not None:
            try:
                info["preview"] = part.related_part(e.get(_R_EMBED)).blob
                break
            except Exception:  # noqa: BLE001
                continue
    return info


_R_DM = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}dm"


def _smartart_items(shape) -> list[str] | None:
    """Text of a SmartArt diagram, in data-model order. python-pptx does not
    expose SmartArt, so before this the whole diagram (often a slide's only
    content) vanished. Returns None when `shape` is not SmartArt."""
    el = getattr(shape, "_element", None)
    if el is None:
        return None
    rid = next((e.get(_R_DM) for e in el.iter() if _local(e.tag) == "relIds" and e.get(_R_DM)), None)
    if not rid:
        return None
    try:
        blob = shape.part.related_part(rid).blob
        try:
            from defusedxml.ElementTree import fromstring  # type: ignore
        except ImportError:  # defusedxml is in the lightweight tier; stdlib fallback
            from xml.etree.ElementTree import fromstring
        root = fromstring(blob)
    except Exception:  # noqa: BLE001
        return []
    items: list[str] = []
    for pt in root.iter():
        if _local(pt.tag) != "pt" or pt.get("type") in ("pres", "parTrans", "sibTrans", "doc"):
            continue
        paras = []
        for para in pt.iter():
            if _local(para.tag) == "p":
                t = "".join(x.text or "" for x in para.iter() if _local(x.tag) == "t").strip()
                if t:
                    paras.append(t)
        if paras:
            items.append(" / ".join(paras))
    return items


_CHART_MAX_ROWS = 200
_CHART_MAX_SERIES = 20


def _chart_md(shape) -> tuple[str, str | None]:
    """Render a chart from the values cached in its XML: a title line and a
    category × series table. Returns (markdown, chart-data workbook member or
    None). Raises on charts python-pptx cannot read; the caller falls back."""
    chart = shape.chart
    title = ""
    try:
        if chart.has_title and chart.chart_title.has_text_frame:
            title = sanitize_heading(chart.chart_title.text_frame.text, maxlen=160)
    except Exception:  # noqa: BLE001
        pass
    try:
        ctype = str(chart.chart_type).split(".")[-1].split(" ")[0].lower().replace("_", " ")
    except Exception:  # noqa: BLE001
        ctype = "chart"
    lines = [f"**Chart{': ' + title if title else ''}** ({ctype})"]

    def cell(v) -> str:
        if v is None:
            return ""
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return str(v).replace("|", "\\|").replace("\n", " ")

    for plot in chart.plots:
        series = list(plot.series)[:_CHART_MAX_SERIES]
        try:
            cats = list(plot.categories.flattened_labels) if plot.categories.depth > 1 else list(plot.categories)
            cats = [" / ".join(map(str, c)) if isinstance(c, tuple) else str(c) for c in cats]
        except Exception:  # noqa: BLE001
            cats = []
        n = max([len(cats)] + [len(s.values) for s in series]) if series else 0
        if not series or n == 0:
            continue
        header = ["category"] + [cell(s.name) or f"series {i}" for i, s in enumerate(series, 1)]
        lines += ["", "| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * len(header)) + " |"]
        for j in range(min(n, _CHART_MAX_ROWS)):
            row = [cell(cats[j]) if j < len(cats) else str(j + 1)]
            row += [cell(s.values[j]) if j < len(s.values) else "" for s in series]
            lines.append("| " + " | ".join(row) + " |")
        if n > _CHART_MAX_ROWS:
            lines.append(f"> **[truncated]** {n - _CHART_MAX_ROWS} more categories")
    member = None
    try:
        wb = shape.chart_part.chart_workbook.xlsx_part
        if wb is not None:
            member = str(wb.partname).lstrip("/")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines), member


class _SlideCtx:
    """Mutable per-slide state for the recursive shape walk."""

    def __init__(self, idx: int, title_shape_id):
        self.idx = idx
        self.title_shape_id = title_shape_id
        self.body: list[str] = []
        self.tables: list[str] = []
        self.pic_idx = 0


def extract(
    input_pptx: Path,
    doc_id: str = "doc-000",
    assets_dir: Path | None = None,
    assets_rel_prefix: str = "../assets",
    extract_images: bool = True,
    embed=None,
) -> tuple[str, dict, list[str]]:
    """Returns (body, extras, warnings). `embed` is an optional
    `_embedded.EmbedSession`: OLE objects and chart-data workbooks get an
    in-place link on their slide, and the session's `apply()` is run on the
    body (the caller adds its frontmatter fields)."""
    Presentation, MSO_SHAPE_TYPE = _try_import()
    warnings: list[str] = []
    extras: dict = {
        "slides": 0,
        "has_notes": False,
        "notes_chars": 0,
        "inline_images": 0,
        "has_tables": False,
        "has_charts": False,
    }

    try:
        prs = Presentation(str(input_pptx))
    except Exception as e:
        raise RuntimeError(f"failed to open pptx: {e}") from e

    extras["slides"] = len(prs.slides)
    pieces: list[str] = []
    counts = {"images": 0, "tables": 0, "charts": 0, "charts_failed": 0, "ole": 0, "smartart": 0}
    total_notes = 0
    asset_relpaths: list[str] = []
    dedup_cache: dict = {}

    do_extract = extract_images and assets_dir is not None
    if do_extract:
        try:
            assets_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            warnings.append(
                f"cannot create assets dir {assets_dir}: {e} — falling back to count-only"
            )
            do_extract = False
    if do_extract:
        try:
            import _raster  # noqa: E402
            _raster.prefetch_zip_media(input_pptx)  # one LibreOffice run for all EMF/WMF
        except Exception:  # noqa: BLE001 — prefetch is only a speed-up
            pass

    def add_image(ctx: _SlideCtx, saved) -> None:
        if saved is not None:
            md_ref, relpath = saved
            ctx.body.append(md_ref)
            if relpath not in asset_relpaths:
                asset_relpaths.append(relpath)
        else:
            ctx.body.append(f"*[Slide {ctx.idx} picture {ctx.pic_idx} skipped]*")

    def walk(shapes, ctx: _SlideCtx) -> None:
        for shape in shapes:
            try:
                stype = shape.shape_type
            except Exception:
                stype = None

            if _is_chrome_placeholder(shape):
                continue

            # Pictures (regular + placeholder-pictures, e.g. "Picture 2"
            # filling a content placeholder on diagram-only slides).
            if stype == MSO_SHAPE_TYPE.PICTURE or _is_picture_like(shape):
                counts["images"] += 1
                ctx.pic_idx += 1
                if do_extract:
                    add_image(ctx, _save_picture_shape(
                        shape, doc_id, ctx.idx, ctx.pic_idx,
                        assets_dir, assets_rel_prefix, dedup_cache, warnings))
                continue

            if getattr(shape, "has_table", False):
                counts["tables"] += 1
                md = _table_md(shape)
                if md:
                    ctx.tables.append(md)
                continue

            if stype == MSO_SHAPE_TYPE.CHART or getattr(shape, "has_chart", False):
                counts["charts"] += 1
                try:
                    md, member = _chart_md(shape)
                except Exception:  # noqa: BLE001
                    counts["charts_failed"] += 1
                    md, member = "*(chart)*", None
                ctx.body.append(md)
                if member and embed is not None:
                    ctx.body.append(embed.place(member, f"slide {ctx.idx}, chart data"))
                continue

            smart = _smartart_items(shape)
            if smart is not None:
                counts["smartart"] += 1
                ctx.body.append("*(SmartArt)*" + ("\n\n" + "\n".join(f"- {t}" for t in smart)
                                                  if smart else " — no text"))
                continue

            ole = _ole_info(shape)
            if ole is not None:
                counts["ole"] += 1
                if ole["preview"] and not ole["icon"] and do_extract:
                    ctx.pic_idx += 1
                    caption = f"{ole['prog_id'] or 'OLE'} object on slide {ctx.idx}"
                    saved = _save_image_blob(
                        ole["preview"], "emf", caption, doc_id, ctx.idx, ctx.pic_idx,
                        assets_dir, assets_rel_prefix, dedup_cache, warnings)
                    if saved is not None:
                        add_image(ctx, saved)
                if ole["member"] and embed is not None:
                    ctx.body.append(embed.place(ole["member"], f"slide {ctx.idx}"))
                elif ole["member"] is None:
                    ctx.body.append(f"*(linked {ole['prog_id'] or 'OLE'} object — the file "
                                    "lives outside the presentation)*")
                continue

            if getattr(shape, "has_text_frame", False):
                txt = _shape_text(shape)
                if ctx.title_shape_id is not None and shape.shape_id == ctx.title_shape_id:
                    # The header holds the first line; later lines are content.
                    txt = txt.strip().split("\n", 1)[1] if "\n" in txt.strip() else ""
                if txt.strip():
                    ctx.body.append(txt.strip())
                continue

            # Groups — recurse to any depth (diagrams are often groups of
            # groups; text, pictures, tables and charts inside all count).
            if stype == MSO_SHAPE_TYPE.GROUP:
                try:
                    walk(shape.shapes, ctx)
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"slide {ctx.idx}: group shape unreadable ({e})")

    for idx, slide in enumerate(prs.slides, start=1):
        try:
            _unwrap_alternate_content(slide.shapes._spTree)
        except Exception as e:  # noqa: BLE001 — never lose the rest of the slide
            warnings.append(f"slide {idx}: alternate-content shapes unreadable ({e})")
        title = _best_title(slide)
        if title:
            title = sanitize_heading(title)
        header = f"## Slide {idx}"
        if title:
            header += f": {title}"
        slide_parts: list[str] = [header, ""]

        title_shape_id = None
        # Find the title shape id so we don't print it twice.
        for shape in slide.shapes:
            try:
                if shape.is_placeholder and shape.placeholder_format.idx == 0:
                    title_shape_id = shape.shape_id
                    break
            except Exception:
                pass

        ctx = _SlideCtx(idx, title_shape_id)
        walk(slide.shapes, ctx)

        if ctx.body:
            slide_parts.append("\n\n".join(ctx.body))
        for tbl in ctx.tables:
            slide_parts.append("")
            slide_parts.append(tbl)

        # Speaker notes.
        notes_text = ""
        if slide.has_notes_slide:
            try:
                notes_text = _tf_text(slide.notes_slide.notes_text_frame).strip()
            except Exception:
                notes_text = ""
        if notes_text:
            extras["has_notes"] = True
            total_notes += len(notes_text)
            slide_parts.append("")
            slide_parts.append("### Notes")
            slide_parts.append("")
            slide_parts.append(notes_text)

        pieces.append("\n".join(slide_parts).rstrip())

    body = "\n\n---\n\n".join(pieces).strip() + "\n"
    body = clean_whitespace(body)

    extras["notes_chars"] = total_notes
    extras["inline_images"] = counts["images"]
    extras["has_tables"] = counts["tables"] > 0
    extras["has_charts"] = counts["charts"] > 0
    if counts["charts_failed"]:
        warnings.append(f"{counts['charts_failed']} chart(s) unreadable "
                        "(rendered as *(chart)* placeholder)")

    # Collect slide titles only, stripping the "Slide N: " chrome prefix so
    # `headings` is consistent with how other extractors record headings
    # (the actual title text, not the structural anchor).
    headings = []
    for line in body.split("\n"):
        if line.startswith("## ") and len(line) < 200:
            heading = line[3:]
            # "Slide N: Title" → "Title"; bare "Slide N" → skip (no title).
            m = re.match(r"^Slide \d+(?::\s*(.+))?$", heading)
            if m:
                title = (m.group(1) or "").strip()
                if not title:
                    continue
                heading = title
            sanitized = sanitize_heading(heading)
            if sanitized:
                headings.append(sanitized)
            if len(headings) >= 15:
                break
    extras["headings"] = headings

    if embed is not None:
        embed.run_ooxml(input_pptx)
        body = clean_whitespace(embed.apply(body))
        for a in re.findall(r"\]\((" + re.escape(assets_rel_prefix) + r"/[^)]+)\)", body):
            if a not in asset_relpaths:
                asset_relpaths.append(a)
    if asset_relpaths:
        extras["assets"] = asset_relpaths

    return body, extras, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract PPTX → Markdown via python-pptx.")
    ap.add_argument("input_pptx")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    add_assets_args(ap)
    args = ap.parse_args()

    in_path = Path(args.input_pptx).expanduser().resolve()
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

    # Default layout: <kb_dir>/docs/*.md + <kb_dir>/assets/.
    assets_dir: Path | None = resolve_assets_dir(args, out_path)

    embed = EmbedSession(input_path=in_path, doc_id=args.doc_id, source_rel=source_rel,
                         out_path=out_path, assets_dir=assets_dir, assets_rel=args.assets_rel)
    try:
        body, extras, warnings = extract(
            in_path,
            doc_id=args.doc_id,
            assets_dir=assets_dir,
            assets_rel_prefix=args.assets_rel,
            extract_images=not args.no_extract_images,
            embed=embed,
        )
    except Exception as e:
        emit_failure(f"extraction failed: {e}", extra={"input": str(in_path)})
        return 1
    warnings.extend(embed.warnings)

    fm = {
        "id": args.doc_id,
        "source": source_rel,
        "source_type": "pptx",
        "source_sha256": sha256_of(in_path),
        "extraction_method": f"{EXTRACTOR_NAME}@{tool_version_string()}",
        "extraction_date": today_iso(),
        "slides": extras.get("slides", 0),
        "has_notes": extras.get("has_notes", False),
        "notes_chars": extras.get("notes_chars", 0),
        "inline_images": extras.get("inline_images", 0),
        "has_tables": extras.get("has_tables", False),
        "has_charts": extras.get("has_charts", False),
        "headings": extras.get("headings", []),
        "tokens_estimated": count_tokens(body),
        "warnings": warnings,
    }
    if extras.get("assets"):
        fm["assets"] = extras["assets"]
    fm.update(embed.frontmatter())
    write_md(out_path, fm, body)
    emit_success(out_path, body, warnings, extra={
        "slides": extras.get("slides", 0),
        "has_notes": extras.get("has_notes", False),
        "assets_extracted": len(extras.get("assets", [])),
        **embed.stdout_extra(),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
