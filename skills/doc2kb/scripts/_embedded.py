"""
_embedded.py — recursive extraction of documents embedded inside documents.

A slide deck can carry a Word file, a workbook or a PDF as an OLE object; a
Word file can carry a deck; a PDF can carry attachments. Before this module
doc2kb read only the outer document, so everything inside those objects was
lost without a word. Now each container extractor (PPTX, DOCX/DOC, XLSX,
PDF) hands its embedded objects to an `EmbedSession`, which

  1. enumerates them — `ppt|word|xl/embeddings/*` members of an OOXML
     package, `EmbeddedFiles` and file-attachment annotations of a PDF —
     and unwraps OLE2 containers (`oleObject*.bin`: OLE Package /
     `\\x01Ole10Native`, `Package`, Acrobat `CONTENTS`, a whole `.doc`);
  2. classifies each payload by magic bytes (never by the declared name);
  3. runs the regular extractor for its type as a child process, which
     writes its own `docs/<parent-id>_eNN-<slug>.md` and recurses into its
     own embeddings the same way;
  4. links every child from the parent body (in place for PPTX OLE shapes
     and charts, otherwise in a closing `## Embedded objects` section) and
     records the children in the parent's frontmatter.

Children are separate documents, not subsections, so each keeps its own
`source_sha256` (of the embedded bytes), `extraction_method`, warnings and
`[page N]` anchors, and the search index cites it on its own:
`source` is the chain `<parent source>#<member inside the parent>`.

Limits (every cut is a warning in the parent, never silent):
    depth       DOC2KB_EMBED_DEPTH (default 4; 0 disables recursion). The top
                document is level 0, its embedded objects level 1, …
    objects     at most MAX_OBJECTS_PER_DOC examined per document
    tree        at most MAX_TREE_DOCS child documents per top-level document
    size        a payload over MAX_OBJECT_BYTES (uncompressed) is refused; a
                zip member compressed more than MAX_RATIO:1 is refused
    time        children share the parent's deadline (DOC2KB_EMBED_DEADLINE,
                set by extract_corpus from --timeout); a child is killed with
                its whole process group when it runs out
    cycles      a payload whose sha256 equals an ancestor's is not re-entered;
                the same payload twice in one parent is extracted once

The recursion state travels to child processes in environment variables
(`DOC2KB_EMBED_*`), so extractor CLIs need no new flags.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from _common import (  # noqa: E402
    kb_doc_filename,
    render_frontmatter,
    sanitize_heading,
    save_image_safe,
    sha256_of,
    skipped_image_note,
    validate_source_rel,
)

SCRIPTS_DIR = Path(__file__).resolve().parent

ENV_DEPTH = "DOC2KB_EMBED_DEPTH"
ENV_LEVEL = "DOC2KB_EMBED_LEVEL"
ENV_CHAIN = "DOC2KB_EMBED_CHAIN"
ENV_DEADLINE = "DOC2KB_EMBED_DEADLINE"
ENV_BUDGET = "DOC2KB_EMBED_BUDGET"
ENV_KEYS = (ENV_LEVEL, ENV_CHAIN, ENV_DEADLINE, ENV_BUDGET)

DEFAULT_DEPTH = 4
MAX_DEPTH = 8
MAX_OBJECTS_PER_DOC = 64
MAX_TREE_DOCS = 200
MAX_OBJECT_BYTES = 100 * 1024 * 1024
MAX_CONTAINER_READ = 512 * 1024 * 1024
MAX_RATIO = 200
MAX_RELS_BYTES = 4 * 1024 * 1024
MAX_ZIP_MEMBERS = 20000
DEFAULT_TIME_BUDGET = 540
MIN_CHILD_SECONDS = 15

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_EMBED_MEMBER_RE = re.compile(r"^(?:word|ppt|xl)/embeddings/[^/]+$", re.IGNORECASE)
_MARKER_FMT = "@@DOC2KB-EMBED-{:03d}@@"
_MARKER_RE = re.compile(r"@@DOC2KB-EMBED-(\d{3})@@")

# kind → (extractor script, extra args). The same scripts extract_corpus.py
# dispatches top-level files to; embedded files never route to the opt-in
# mineru tier (a scanned PDF inside a deck surfaces as a short extraction).
KIND_SCRIPTS: dict[str, tuple[str, list[str]]] = {
    "pdf": ("extract_pdf_pymupdf4llm.py", []),
    "docx": ("extract_docx.py", []),
    "doc": ("extract_doc.py", []),
    "pptx": ("extract_pptx.py", []),
    "xlsx": ("extract_xlsx.py", []),
    "rtf": ("extract_rtf.py", []),
    "epub": ("extract_epub.py", []),
    "html": ("extract_html.py", []),
    "xml": ("extract_xml.py", []),
    "ipynb": ("extract_ipynb.py", []),
    "md": ("extract_md_txt.py", ["--mode", "md"]),
    "txt": ("extract_md_txt.py", ["--mode", "txt"]),
}
KIND_EXT = {"pdf": "pdf", "docx": "docx", "doc": "doc", "pptx": "pptx", "xlsx": "xlsx",
            "rtf": "rtf", "epub": "epub", "html": "html", "xml": "xml", "ipynb": "ipynb",
            "md": "md", "txt": "txt"}
_TEXT_EXT_KIND = {"txt": "txt", "text": "txt", "csv": "txt", "tsv": "txt", "log": "txt",
                  "json": "txt", "md": "md", "markdown": "md", "html": "html", "htm": "html",
                  "xhtml": "html", "xml": "xml", "ipynb": "ipynb"}
_UNSUPPORTED = {
    "xls": "legacy Excel workbook (.xls) — not supported",
    "ppt": "legacy PowerPoint deck (.ppt) — not supported",
    "visio": "Visio drawing — not supported (its preview image, if any, is kept)",
    "odf": "OpenDocument file — not supported",
    "zip": "zip archive — not unpacked",
    "equation": "equation object — its preview image is the only rendering",
}


@dataclass
class EmbeddedObject:
    """One object found inside a parent document."""
    member: str                 # where it lives in the parent ('ppt/embeddings/oleObject1.bin')
    name: str                   # display file name
    data: bytes | None = None
    location: str = ""          # 'slide 5', 'page 3', 'chart data', …
    prog_id: str = ""
    kind: str = ""
    note: str = ""              # why it is not extracted (empty when it is)
    key: str = ""               # 'e01'
    size: int = 0
    sha256: str = ""
    status: str = ""            # extracted | image | duplicate | skipped | failed
    child_id: str = ""
    child_doc: str = ""         # file name under docs/
    image_ref: str = ""
    inner: str = ""             # file name an OLE Package stored for the payload
    child_warnings: int = 0
    nested_cuts: int = 0        # objects deeper in this child that were not extracted
    warn: bool = True           # surface `note` as a parent warning
    loader: Callable[[], bytes] | None = None  # lazy read (OOXML members)


# ---------- small helpers ----------

def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(str(os.environ.get(name, "")).strip())
    except ValueError:
        return default
    return max(lo, min(hi, v))


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _clean_member(member: str) -> str:
    """A member path safe to append to `source` (no '..', no control chars)."""
    member = re.sub(r"[\x00-\x1f\x7f]", "", member.replace("\\", "/"))
    parts = [p for p in member.split("/") if p not in ("", ".", "..")]
    return "/".join(parts)[:240] or "object"


def _clean_name(name: str, fallback: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", (name or "").replace("\\", "/")).rsplit("/", 1)[-1].strip()
    return sanitize_heading(name, maxlen=120) or fallback


def _xml_root(data: bytes):
    try:
        from defusedxml.ElementTree import fromstring  # type: ignore
    except ImportError:
        from xml.etree.ElementTree import fromstring
    return fromstring(data)


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


# ---------- payload classification ----------

def _image_ext(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:2] == b"BM":
        return "bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    try:
        import _raster  # noqa: E402
        fmt = _raster.sniff_vector_format(data)
    except Exception:  # noqa: BLE001
        fmt = None
    return fmt if fmt in ("emf", "wmf", "svg", "eps") else None


def _zip_kind(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_MEMBERS:
                return "zip"
            names = {i.filename.lower() for i in infos}
            if "word/document.xml" in names:
                return "docx"
            if "ppt/presentation.xml" in names:
                return "pptx"
            if "xl/workbook.xml" in names:
                return "xlsx"
            if "visio/document.xml" in names:
                return "visio"
            if "mimetype" in names:
                info = zf.getinfo(next(i.filename for i in infos if i.filename.lower() == "mimetype"))
                if info.file_size <= 256:
                    mt = zf.read(info).decode("ascii", "replace").strip()
                    if mt == "application/epub+zip":
                        return "epub"
                    if mt.startswith("application/vnd.oasis.opendocument"):
                        return "odf"
    except Exception:  # noqa: BLE001 — a broken zip is just "not a document"
        return "zip"
    return "zip"


def detect_kind(name: str, data: bytes) -> str:
    """Classify a payload by its bytes (the name only breaks ties for plain
    text). Returns a KIND_SCRIPTS key, 'image', 'ole' or an _UNSUPPORTED key /
    'unsupported'."""
    if data[:5] == b"%PDF-" or b"%PDF-" in data[:1024]:
        return "pdf"
    if data[:8] == OLE_MAGIC:
        return "ole"
    if data[:4] == b"PK\x03\x04":
        return _zip_kind(data)
    if data[:5] == b"{\\rtf":
        return "rtf"
    if _image_ext(data):
        return "image"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in _TEXT_EXT_KIND and b"\x00" not in data[:4096]:
        return _TEXT_EXT_KIND[ext]
    return "unsupported"


# ---------- OLE2 unwrapping ----------

def _u32(b: bytes, pos: int) -> int:
    return struct.unpack_from("<I", b, pos)[0]


def _cstr(b: bytes, pos: int, limit: int = 4096) -> tuple[bytes, int]:
    end = b.find(b"\x00", pos, pos + limit)
    if end < 0:
        raise ValueError("unterminated string")
    return b[pos:end], end + 1


def _decode_ansi(raw: bytes) -> str:
    for enc in ("utf-8", "cp1251", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def parse_ole10native(stream: bytes) -> tuple[str, bytes]:
    """Parse an OLE Package `\\x01Ole10Native` stream → (file name, file bytes).
    Layout: u32 total, u16 type, label\\0, source path\\0, u32 unknown,
    u32 len + temp path, u32 len + data, then optional UTF-16 copies of the
    three strings. Raises ValueError on anything out of bounds."""
    if len(stream) < 6:
        raise ValueError("Ole10Native stream too short")
    pos = 4
    if struct.unpack_from("<H", stream, pos)[0] != 2:
        # Not a Package: the native data simply follows the size field.
        total = _u32(stream, 0)
        if 4 + total > len(stream):
            raise ValueError("Ole10Native size out of bounds")
        return "", stream[4:4 + total]
    pos += 2
    label, pos = _cstr(stream, pos)
    src_path, pos = _cstr(stream, pos)
    pos += 4
    tlen = _u32(stream, pos)
    pos += 4
    if pos + tlen > len(stream):
        raise ValueError("Ole10Native temp path out of bounds")
    tmp_path = stream[pos:pos + tlen].rstrip(b"\x00")
    pos += tlen
    dlen = _u32(stream, pos)
    pos += 4
    if pos + dlen > len(stream):
        raise ValueError("Ole10Native data out of bounds")
    data = stream[pos:pos + dlen]
    pos += dlen
    names = [_decode_ansi(x) for x in (label, src_path, tmp_path) if x]
    # Optional Unicode trailer: tmp path, label, source path (u32 char count each).
    try:
        uni: list[str] = []
        for _ in range(3):
            n = _u32(stream, pos)
            pos += 4
            if n > 4096 or pos + 2 * n > len(stream):
                break
            uni.append(stream[pos:pos + 2 * n].decode("utf-16-le", "replace").rstrip("\x00"))
            pos += 2 * n
        if len(uni) >= 2 and uni[1]:
            names.insert(0, uni[1])
    except struct.error:
        pass
    name = next((n for n in names if n.strip()), "")
    return name, data


def _compobj_prog_id(ole) -> str:
    """ProgID (or user type) from the `\\x01CompObj` stream, '' if absent."""
    try:
        if not ole.exists("\x01CompObj"):
            return ""
        b = ole.openstream("\x01CompObj").read(4096)
        pos = 28
        strings: list[str] = []
        for _ in range(3):
            n = _u32(b, pos)
            pos += 4
            if n in (0xFFFFFFFF, 0xFFFFFFFE):  # clipboard format id, no string
                pos += 4
                strings.append("")
                continue
            if n > 512 or pos + n > len(b):
                break
            strings.append(b[pos:pos + n].rstrip(b"\x00").decode("latin-1"))
            pos += n
        prog = strings[2] if len(strings) > 2 else ""
        user = strings[0] if strings else ""
        return sanitize_heading(prog or user, maxlen=80)
    except Exception:  # noqa: BLE001
        return ""


def unwrap_ole(data: bytes) -> tuple[str, bytes | None, str, str]:
    """Look inside an OLE2 compound file. Returns (name, payload, prog_id,
    note): `payload` is the embedded file's bytes when there is one doc2kb
    can read, else None and `note` says what the object is."""
    try:
        import olefile  # type: ignore
    except ImportError:
        return "", None, "", "olefile is not installed — re-run ensure_env.py to read OLE objects"
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        return "", None, "", f"unreadable OLE container ({str(e)[:80]})"
    try:
        streams = {"/".join(p).lower(): "/".join(p)
                   for p in ole.listdir(streams=True, storages=False)}
        prog = _compobj_prog_id(ole)

        def read(lower: str) -> bytes:
            real = streams[lower]
            if ole.get_size(real) > MAX_OBJECT_BYTES:
                raise ValueError(f"stream {real!r} is over the {MAX_OBJECT_BYTES // 2**20} MiB cap")
            return ole.openstream(real).read()

        if "\x01ole10native" in streams:
            name, payload = parse_ole10native(read("\x01ole10native"))
            return name, payload, prog or "Package", ""
        if "package" in streams:
            return "", read("package"), prog, ""
        if "contents" in streams:
            b = read("contents")
            if b[:5] == b"%PDF-" or b"%PDF-" in b[:1024]:
                return "", b, prog or "AcroExch.Document", ""
        if "worddocument" in streams:
            return "", data, prog or "Word.Document.8", ""
        if "workbook" in streams or "book" in streams:
            return "", None, prog or "Excel.Sheet.8", _UNSUPPORTED["xls"]
        if "powerpoint document" in streams:
            return "", None, prog or "PowerPoint.Show.8", _UNSUPPORTED["ppt"]
        if "equation native" in streams:
            return "", None, prog or "Equation.3", _UNSUPPORTED["equation"]
        if "visiodocument" in streams:
            return "", None, prog or "Visio.Drawing", _UNSUPPORTED["visio"]
        return "", None, prog, f"OLE object ({prog or 'unknown type'}) — not a document doc2kb can read"
    except Exception as e:  # noqa: BLE001
        return "", None, "", f"unreadable OLE object ({str(e)[:80]})"
    finally:
        ole.close()


# ---------- enumeration ----------

def _rels_targets(zf: zipfile.ZipFile, infos) -> tuple[dict[str, str], int]:
    """member → source part that references it, plus the number of external
    (linked, not embedded) OLE objects."""
    owner: dict[str, str] = {}
    linked = 0
    for info in infos:
        name = info.filename
        if not name.endswith(".rels") or "/_rels/" not in "/" + name or info.file_size > MAX_RELS_BYTES:
            continue
        src_dir, _, rels_file = name.rpartition("_rels/")
        source = src_dir + rels_file[: -len(".rels")]
        try:
            root = _xml_root(zf.read(info))
        except Exception:  # noqa: BLE001
            continue
        for rel in root:
            if _local(rel.tag) != "Relationship":
                continue
            target = rel.get("Target") or ""
            rtype = rel.get("Type") or ""
            if rel.get("TargetMode") == "External":
                if rtype.endswith(("/oleObject", "/package")):
                    linked += 1
                continue
            if target.startswith("/"):
                resolved = target.lstrip("/")
            else:
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
            owner.setdefault(resolved.lower(), source)
    return owner, linked


def _location_from_part(source: str) -> str:
    m = re.match(r"^(?:word|ppt|xl)/charts/(chart\d+)\.xml$", source, re.IGNORECASE)
    if m:
        return f"chart data ({m.group(1)})"
    if re.match(r"^word/document\.xml$", source, re.IGNORECASE):
        return "document body"
    m = re.match(r"^word/(header|footer)\d*\.xml$", source, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    m = re.match(r"^xl/worksheets/(sheet\d+)\.xml$", source, re.IGNORECASE)
    if m:
        return m.group(1)
    return source


def collect_ooxml(path: Path) -> tuple[list[EmbeddedObject], list[str]]:
    """Embedded objects of a DOCX/PPTX/XLSX package (`*/embeddings/*`)."""
    objects: list[EmbeddedObject] = []
    warnings: list[str] = []
    try:
        zf = zipfile.ZipFile(path)
    except Exception:  # noqa: BLE001 — not a zip: nothing embedded to find
        return objects, warnings
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            return objects, [f"embedded objects not examined: {len(infos)} zip members (cap {MAX_ZIP_MEMBERS})"]
        members = sorted((i for i in infos if _EMBED_MEMBER_RE.match(i.filename) and not i.is_dir()),
                         key=lambda i: _natural_key(i.filename))
        if not members:
            return objects, warnings
        owner, linked = _rels_targets(zf, infos)
        if linked:
            warnings.append(f"{linked} linked OLE object(s) point to files outside the document — "
                            "not embedded, cannot be extracted")
        declared_total = 0
        for info in members:
            member = _clean_member(info.filename)
            obj = EmbeddedObject(member=member, name=_clean_name(info.filename, "object"),
                                 size=info.file_size,
                                 location=_location_from_part(owner.get(info.filename.lower(), "")))
            if info.flag_bits & 0x1:
                obj.note = "encrypted zip member"
            elif info.file_size > MAX_OBJECT_BYTES:
                obj.note = f"{_human(info.file_size)}, over the {MAX_OBJECT_BYTES // 2**20} MiB cap"
            elif (info.compress_size and info.file_size > 2**20
                  and info.file_size / info.compress_size > MAX_RATIO):
                obj.note = (f"compression ratio {info.file_size // max(1, info.compress_size)}:1 "
                            f"over {MAX_RATIO}:1 — refused as a possible zip bomb")
            elif declared_total + info.file_size > MAX_CONTAINER_READ:
                obj.note = f"container read budget ({MAX_CONTAINER_READ // 2**20} MiB) exhausted"
            else:
                declared_total += info.file_size
                # Read when the session gets to it, one object in memory at a time.
                obj.loader = lambda name=info.filename: _read_member(path, name)
            objects.append(obj)
    return objects, warnings


def _read_member(path: Path, name: str) -> bytes:
    """Bounded read of one zip member; raises ValueError past the cap (the
    declared size can lie)."""
    with zipfile.ZipFile(path) as zf, zf.open(name) as fh:
        data = fh.read(MAX_OBJECT_BYTES + 1)
    if len(data) > MAX_OBJECT_BYTES:
        raise ValueError("larger than its declared size — refused")
    return data


def collect_pdf(path: Path) -> tuple[list[EmbeddedObject], list[str]]:
    """Files embedded in a PDF: the EmbeddedFiles name tree (incl. portfolios)
    and file-attachment annotations."""
    objects: list[EmbeddedObject] = []
    warnings: list[str] = []
    try:
        import pymupdf  # type: ignore
    except ImportError:
        return objects, warnings
    try:
        doc = pymupdf.open(str(path))
    except Exception:  # noqa: BLE001
        return objects, warnings
    try:
        if not doc.is_pdf:
            return objects, warnings
        for i in range(doc.embfile_count()):
            try:
                info = doc.embfile_info(i)
            except Exception:  # noqa: BLE001
                continue
            name = info.get("ufilename") or info.get("filename") or info.get("name") or f"file{i + 1}"
            obj = EmbeddedObject(member=_clean_member(f"EmbeddedFiles/{name}"),
                                 name=_clean_name(name, f"file{i + 1}"),
                                 size=int(info.get("size") or 0),
                                 location="attachment")
            _load_pdf_payload(obj, lambda i=i: doc.embfile_get(i), info.get("length"))
            objects.append(obj)
        for page in doc:
            try:
                annots = list(page.annots(types=[pymupdf.PDF_ANNOT_FILE_ATTACHMENT]))
            except Exception:  # noqa: BLE001
                continue
            for n, annot in enumerate(annots, 1):
                try:
                    info = annot.file_info
                except Exception:  # noqa: BLE001
                    info = {}
                name = info.get("filename") or f"attachment{n}"
                obj = EmbeddedObject(
                    member=_clean_member(f"page{page.number + 1}/attachment{n}/{name}"),
                    name=_clean_name(name, f"attachment{n}"),
                    size=int(info.get("size") or 0),
                    location=f"page {page.number + 1}")
                _load_pdf_payload(obj, annot.get_file, info.get("length"))
                objects.append(obj)
    finally:
        doc.close()
    return objects, warnings


def _load_pdf_payload(obj: EmbeddedObject, getter, length) -> None:
    if obj.size > MAX_OBJECT_BYTES:
        obj.note = f"{_human(obj.size)}, over the {MAX_OBJECT_BYTES // 2**20} MiB cap"
        return
    try:
        length = int(length or 0)
    except (TypeError, ValueError):
        length = 0
    if length and obj.size > 2**20 and obj.size / length > MAX_RATIO:
        obj.note = f"compression ratio over {MAX_RATIO}:1 — refused as a possible bomb"
        return
    try:
        data = getter()
    except Exception as e:  # noqa: BLE001
        obj.note = f"unreadable attachment ({str(e)[:80]})"
        return
    if len(data) > MAX_OBJECT_BYTES:
        obj.note = f"{_human(len(data))}, over the {MAX_OBJECT_BYTES // 2**20} MiB cap"
        return
    obj.data = data
    obj.size = len(data)


# ---------- the session ----------

class EmbedSession:
    """Embedded-object handling for one parent document.

    Typical use inside an extractor::

        emb = EmbedSession(input_path=in_path, doc_id=..., source_rel=..., out_path=...)
        marker = emb.place("ppt/embeddings/x.docx", "slide 4")   # PPTX only
        emb.run_ooxml(in_path)            # or emb.run_pdf(in_path)
        body = emb.apply(body)
        fm.update(emb.frontmatter()); warnings += emb.warnings
    """

    def __init__(self, *, input_path: Path, doc_id: str, source_rel: str, out_path: Path,
                 assets_dir: Path | None = None, assets_rel: str = "../assets"):
        self.input_path = Path(input_path)
        self.doc_id = doc_id
        self.safe_id = re.sub(r"[^A-Za-z0-9_-]", "", str(doc_id)) or "doc"
        self.source_rel = source_rel
        self.out_path = Path(out_path)
        self.docs_dir = self.out_path.parent
        self.assets_dir = assets_dir
        self.assets_rel = assets_rel
        self.max_depth = _env_int(ENV_DEPTH, DEFAULT_DEPTH, 0, MAX_DEPTH)
        self.level = _env_int(ENV_LEVEL, 0, 0, MAX_DEPTH + 1)
        self.chain = [c for c in os.environ.get(ENV_CHAIN, "").split(",") if re.fullmatch(r"[0-9a-f]{64}", c)]
        try:
            self.deadline = float(os.environ[ENV_DEADLINE])
        except (KeyError, ValueError):
            self.deadline = time.time() + DEFAULT_TIME_BUDGET
        self.budget = _env_int(ENV_BUDGET, MAX_TREE_DOCS, 0, MAX_TREE_DOCS)
        self.objects: list[EmbeddedObject] = []
        self.warnings: list[str] = []
        self.docs_total = 0
        self.not_extracted: list[str] = []  # own + descendants, for the top-level summary
        self._placed: dict[str, int] = {}
        self._markers: list[str] = []
        self._sha: str | None = None
        self._cleaned = False

    # -- placement (PPTX knows where an OLE object or chart sits) --

    def place(self, member: str, location: str = "") -> str:
        """Reserve an in-body spot for the object stored at `member` and return
        the marker to put there; `apply()` replaces it with the link."""
        member = _clean_member(member)
        if member not in self._placed:
            self._placed[member] = len(self._markers)
            self._markers.append(location)
        return _MARKER_FMT.format(self._placed[member])

    # -- run --

    @property
    def enabled(self) -> bool:
        return self.max_depth > 0

    def cleanup_stale(self) -> None:
        """Drop child docs/assets left by an earlier extraction of this parent
        (`<id>_e*`): the set of embedded objects may have changed. The `_e`
        separator cannot occur in a scout id or a slug, so only children
        match."""
        if self._cleaned:
            return
        self._cleaned = True
        dirs = [self.docs_dir] + ([self.assets_dir] if self.assets_dir else [])
        for d in dirs:
            if not d or not d.is_dir():
                continue
            for p in d.glob(f"{self.safe_id}_e*"):
                try:
                    if p.is_dir() and not p.is_symlink():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
                except OSError:
                    pass

    def run_ooxml(self, package: Path) -> None:
        self.cleanup_stale()
        if not self.enabled:
            return
        objects, warnings = collect_ooxml(package)
        self.warnings.extend(warnings)
        self.run(objects)

    def run_pdf(self, pdf: Path) -> None:
        self.cleanup_stale()
        if not self.enabled:
            return
        objects, warnings = collect_pdf(pdf)
        self.warnings.extend(warnings)
        self.run(objects)

    def run(self, objects: list[EmbeddedObject]) -> None:
        self.cleanup_stale()
        if not self.enabled or not objects:
            return
        if len(objects) > MAX_OBJECTS_PER_DOC:
            self.warnings.append(f"{len(objects) - MAX_OBJECTS_PER_DOC} embedded object(s) beyond the "
                                 f"first {MAX_OBJECTS_PER_DOC} not examined")
            objects = objects[:MAX_OBJECTS_PER_DOC]
        self._sha = self._sha or sha256_of(self.input_path)
        if self._placed:  # number placed objects in reading order (slide order)
            objects = sorted(objects, key=lambda o: self._placed.get(o.member, len(self._placed)))
            for o in objects:
                if o.member in self._placed and self._markers[self._placed[o.member]]:
                    o.location = self._markers[self._placed[o.member]]
        seen: dict[str, EmbeddedObject] = {}
        for n, obj in enumerate(objects, 1):
            obj.key = f"e{n:02d}"
            self.objects.append(obj)
            self._resolve(obj)
            if obj.data is None or obj.note:
                obj.status = obj.status or "skipped"
                continue
            obj.sha256 = hashlib.sha256(obj.data).hexdigest()
            if obj.sha256 in seen and seen[obj.sha256].status in ("extracted", "image"):
                first = seen[obj.sha256]
                obj.status, obj.child_id, obj.child_doc, obj.image_ref = (
                    "duplicate", first.child_id, first.child_doc, first.image_ref)
                continue
            seen.setdefault(obj.sha256, obj)
            if obj.kind == "image":
                self._save_image(obj)
            else:
                self._extract_child(obj)
            obj.data = None  # free memory as we go
        for obj in self.objects:
            if obj.note and obj.warn:
                self.warnings.append(f"embedded {obj.key} `{obj.name}`"
                                     f"{' (' + obj.location + ')' if obj.location else ''}: "
                                     f"not extracted — {obj.note}")
                self.not_extracted.insert(0, f"{self.doc_id} {obj.key} `{obj.name}`: {obj.note}")

    def _resolve(self, obj: EmbeddedObject) -> None:
        """Unwrap OLE and classify; sets kind/name/data or note."""
        if obj.data is None and obj.loader is not None and not obj.note:
            try:
                obj.data = obj.loader()
            except ValueError as e:
                obj.note = str(e)
            except Exception as e:  # noqa: BLE001 — bad CRC, truncated member, …
                obj.note = f"unreadable zip member ({str(e)[:80]})"
            obj.loader = None
        if obj.data is None:
            return
        kind = detect_kind(obj.name, obj.data)
        for _ in range(2):  # an OLE Package may itself wrap an OLE file (.doc)
            if kind != "ole":
                break
            name, payload, prog, note = unwrap_ole(obj.data)
            obj.prog_id = obj.prog_id or prog
            if payload is None:
                obj.data, obj.note = None, note
                obj.kind = "equation" if note == _UNSUPPORTED["equation"] else "ole"
                obj.warn = obj.kind != "equation"
                return
            if name:
                obj.name = obj.inner = _clean_name(name, obj.name)
            if payload is obj.data:  # the OLE file is the document itself (.doc)
                kind = "doc"
                break
            obj.data = payload
            kind = detect_kind(obj.name, payload)
        if kind == "ole":
            kind = "unsupported"
        obj.kind = kind
        if kind in KIND_SCRIPTS or kind == "image":
            ext = KIND_EXT.get(kind)
            if ext and not obj.name.lower().endswith("." + ext) and obj.name.lower().endswith((".bin", ".tmp")):
                obj.name = obj.name.rsplit(".", 1)[0] + "." + ext
            return
        obj.note = _UNSUPPORTED.get(kind) or (
            f"unsupported embedded type ({obj.name.rsplit('.', 1)[-1].lower() if '.' in obj.name else 'binary'})")
        obj.data = None

    def _save_image(self, obj: EmbeddedObject) -> None:
        if self.assets_dir is None:
            obj.note, obj.warn = "image extraction disabled", False
            return
        ext = _image_ext(obj.data or b"") or "png"
        try:
            self.assets_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            obj.note = f"cannot create assets dir ({e})"
            return
        res = save_image_safe(obj.data or b"", self.assets_dir / f"{self.safe_id}_{obj.key}.{ext}",
                              source_ext=ext)
        if not res:
            obj.note = skipped_image_note(res, ext) or "image too small or empty"
            obj.warn = res.reason != "skipped_too_small"
            return
        obj.status = "image"
        obj.image_ref = f"![{obj.name}]({self.assets_rel}/{res.path.name})"

    def _extract_child(self, obj: EmbeddedObject) -> None:
        if self.level + 1 > self.max_depth:
            obj.note = f"embedding depth limit ({self.max_depth}) reached — raise {ENV_DEPTH} / --embed-depth to go deeper"
            return
        if obj.sha256 in self.chain or obj.sha256 == self._sha:
            obj.note = "contains one of its own ancestors (cycle) — not re-entered"
            return
        if self.budget < 1:
            obj.note = f"embedded-document budget ({MAX_TREE_DOCS} per top-level file) exhausted"
            return
        remaining = self.deadline - time.time()
        if remaining < MIN_CHILD_SECONDS:
            obj.note = "time budget for embedded documents exhausted"
            return
        script, extra = KIND_SCRIPTS[obj.kind]
        child_id = f"{self.safe_id}_{obj.key}"
        child_source = f"{self.source_rel}#{obj.member}" + (f"/{obj.inner}" if obj.inner else "")
        try:
            child_source = validate_source_rel(child_source)
        except ValueError as e:
            obj.note = f"unusable source path ({e})"
            return
        out_md = self.docs_dir / kb_doc_filename(child_id, obj.name)
        env = dict(os.environ)
        env[ENV_DEPTH] = str(self.max_depth)
        env[ENV_LEVEL] = str(self.level + 1)
        env[ENV_CHAIN] = ",".join(self.chain + [self._sha or "", obj.sha256])
        env[ENV_DEADLINE] = f"{self.deadline:.3f}"
        env[ENV_BUDGET] = str(self.budget - 1)
        work = Path(tempfile.mkdtemp(prefix="doc2kb-embed-"))
        try:
            src = work / f"embedded.{KIND_EXT[obj.kind]}"
            src.write_bytes(obj.data or b"")
            argv = [sys.executable, str(SCRIPTS_DIR / script), str(src), str(out_md),
                    "--doc-id", child_id, "--source-rel", child_source, *extra]
            from _raster import _run_killable  # noqa: E402 — process-group kill on timeout
            try:
                proc = _run_killable(argv, remaining, env=env, cwd=str(work))
            except subprocess.TimeoutExpired:
                obj.status, obj.note = "failed", f"{obj.kind} extractor timed out"
                return
            parsed = _last_json(proc.stdout.decode("utf-8", "replace"))
            if proc.returncode == 2:
                reason = (parsed or {}).get("reason") or "a system converter is missing"
                obj.status, obj.note = "failed", f"needs install: {str(reason)[:200]}"
                return
            if proc.returncode != 0 or not parsed or parsed.get("ok") is not True:
                reason = (parsed or {}).get("reason") if parsed else None
                if not reason:
                    tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
                    reason = f"exit {proc.returncode}: {tail[0][:160]}"
                obj.status, obj.note = "failed", f"{obj.kind} extractor failed: {str(reason)[:200]}"
                return
            written = Path(parsed.get("out") or out_md)
            if written.resolve().parent != self.docs_dir.resolve() or not written.is_file():
                obj.status, obj.note = "failed", "child wrote outside docs/"
                return
            _stamp_child(written, {
                "embedded_in": self.doc_id,
                "embedded_member": obj.member,
                "embedded_name": obj.name,
                "embedded_location": obj.location or None,
                "embedded_depth": self.level + 1,
                "parent_source_sha256": self._sha,
            })
            obj.status = "extracted"
            obj.child_id = child_id
            obj.child_doc = written.name
            obj.child_warnings = len(parsed.get("warnings") or [])
            used = 1 + int(parsed.get("embedded_docs_total") or 0)
            self.budget -= used
            self.docs_total += used
            nested = [str(x) for x in (parsed.get("embedded_not_extracted") or [])][:50]
            obj.nested_cuts = len(nested)
            self.not_extracted.extend(nested)
            if obj.child_warnings:
                msg = (f"embedded {obj.key} ({child_id}): {obj.child_warnings} "
                       f"warning(s) — see docs/{written.name}")
                if nested:
                    msg += f"; {len(nested)} object(s) inside it not extracted, e.g. {nested[0]}"
                self.warnings.append(msg)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # -- rendering --

    def _describe(self, obj: EmbeddedObject, with_location: bool) -> str:
        bits = []
        if obj.kind and obj.kind not in ("ole", "unsupported"):
            bits.append(obj.kind)
        if obj.prog_id:
            bits.append(obj.prog_id)
        if obj.size:
            bits.append(_human(obj.size))
        if with_location and obj.location:
            bits.append(obj.location)
        head = f"**{obj.key}** `{obj.name}`" + (f" ({', '.join(bits)})" if bits else "")
        if obj.status in ("extracted", "duplicate") and obj.child_doc:
            same = " (same content as an earlier object)" if obj.status == "duplicate" else ""
            return f"{head} → [{obj.child_id}](../docs/{obj.child_doc}){same}"
        if obj.status in ("image", "duplicate") and obj.image_ref:
            return f"{head}\n\n{obj.image_ref}"
        return f"{head} — not extracted: {obj.note or 'unknown reason'}"

    def apply(self, body: str) -> str:
        """Replace placement markers with links and append the objects that
        had no in-body spot under `## Embedded objects`."""
        by_member = {o.member: o for o in self.objects}
        placed_keys: set[str] = set()

        def repl(m: re.Match) -> str:
            idx = int(m.group(1))
            member = next((k for k, v in self._placed.items() if v == idx), None)
            obj = by_member.get(member) if member else None
            if obj is None:
                return ""
            placed_keys.add(obj.key)
            return "> Embedded " + self._describe(obj, with_location=False).replace("\n\n", "\n>\n> ")

        body = _MARKER_RE.sub(repl, body)
        body = re.sub(r"\n{3,}", "\n\n", body)
        rest = [o for o in self.objects if o.key not in placed_keys]
        if rest:
            lines = ["", "## Embedded objects", ""]
            for o in rest:
                lines.append("- " + self._describe(o, with_location=True).replace("\n\n", "\n\n  "))
            body = body.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
        return body

    def apply_to_file(self, path: Path) -> None:
        """For an extractor that writes its doc before the embedded objects are
        handled (XLSX: the openpyxl child writes it): append the section and
        merge frontmatter + warnings into the file on disk."""
        if not self.objects and not self.warnings:
            return
        from _common import FRONTMATTER_RE, count_tokens, parse_frontmatter_text  # noqa: E402
        text = path.read_text(encoding="utf-8")
        m = FRONTMATTER_RE.match(text)
        if not m:
            return
        fm = parse_frontmatter_text(text)
        body = text[m.end():]
        new_body = self.apply(body)
        added = new_body[len(body.rstrip("\n")):] if new_body.startswith(body.rstrip("\n")) else new_body
        fm["warnings"] = list(fm.get("warnings") or []) + self.warnings
        fm.update(self.frontmatter())
        if isinstance(fm.get("tokens_estimated"), int):
            fm["tokens_estimated"] += count_tokens(added)
        path.write_text(render_frontmatter(fm) + new_body, encoding="utf-8")

    def frontmatter(self) -> dict:
        if not self.objects:
            return {}
        return {
            "embedded_objects": len(self.objects),
            "embedded_docs": [o.child_id for o in self.objects if o.status == "extracted"],
        }

    def stdout_extra(self) -> dict:
        return {"embedded_objects": len(self.objects), "embedded_docs_total": self.docs_total,
                "embedded_not_extracted": self.not_extracted[:50]}


def _last_json(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def _stamp_child(path: Path, fields: dict) -> None:
    """Insert provenance keys into the child's frontmatter, right after its
    `source:` line (or the opening `---` if there is none)."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return
    rendered = render_frontmatter({k: v for k, v in fields.items() if v is not None})
    inner = "\n".join(rendered.split("\n")[1:-2]) + "\n"  # drop '---' lines
    end = text.find("\n---\n", 3)
    m = re.search(r"^source: [^\n]*\n", text[:end if end > 0 else len(text)], re.MULTILINE)
    at = m.end() if m else 4
    path.write_text(text[:at] + inner + text[at:], encoding="utf-8")
