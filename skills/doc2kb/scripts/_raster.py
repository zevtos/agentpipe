"""
_raster.py — turn vector / metafile images into PNG for doc2kb's image pipeline.

Office documents keep a lot of their diagrams as EMF/WMF metafiles (Visio
pastes, "vector clipart", old equation previews), EPUBs ship SVG, and some
DOCX files carry a PDF or EPS as an image. Claude's vision encoder reads
none of these, so before this module `save_image_safe()` skipped them and the
diagram was lost. Now every vector image is rasterized to PNG and saved like
any other picture.

Routes (all system tools are optional; a missing one degrades to the old
skip, with an install hint in the warning — never a silent loss):

    emf / wmf  LibreOffice (`soffice --headless --convert-to pdf`) → PDF,
               then PyMuPDF renders it. One soffice run per document via
               `prefetch()` (≈2 s for ten metafiles instead of ≈1.5 s each).
    svg        PyMuPDF opens SVG directly (external references and entity
               declarations are refused first).
    pdf        PyMuPDF renders page 1.
    eps        Ghostscript (`gs -dSAFER … -sDEVICE=pdfwrite -dEPSCrop`) → PDF,
               else the TIFF/WMF preview a DOS-binary EPS carries.

The rendered page is cropped to its drawn content (LibreOffice places a
metafile on an A4 page) and scaled so its longer side is RASTER_TARGET_DIM px.

No pip dependency beyond the lightweight tier: PyMuPDF and Pillow are
already there; LibreOffice and Ghostscript are system tools like the `.doc`
converter. This module imports nothing from `_common` so `_common` can import
it lazily.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import struct
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Iterable

VECTOR_FORMATS = ("emf", "wmf", "svg", "pdf", "eps")

# Longer side of the rendered PNG. Below save_image_safe's 2000-px cap, large
# enough that 8-pt diagram labels on an A4-wide figure stay legible.
RASTER_TARGET_DIM = 1600
# Never zoom past this (relative to 72 dpi): a 20-pt icon must not become a
# 1600-px blur.
MAX_ZOOM = 6.0
MAX_VECTOR_BYTES = 64 * 1024 * 1024

SOFFICE_TIMEOUT_BASE = 60
SOFFICE_TIMEOUT_PER_ITEM = 10
SOFFICE_TIMEOUT_CAP = 300
GS_TIMEOUT = 60

LIBREOFFICE_HINT = (
    "LibreOffice is not on PATH — install it to rasterize EMF/WMF "
    "(`brew install --cask libreoffice` / `apt install libreoffice-draw`, provides `soffice`)"
)
GHOSTSCRIPT_HINT = (
    "Ghostscript is not on PATH — install it to rasterize EPS "
    "(`brew install ghostscript` / `apt install ghostscript`)"
)

# sha256(blob) → (png bytes or None, detail). Per process: an extractor run
# is one document, so this never grows past that document's images.
_CACHE: dict[str, tuple[bytes | None, str]] = {}


# ---------- format sniffing ----------

_WMF_STD_RE = re.compile(rb"^[\x01\x02]\x00\x09\x00\x00[\x01\x03]")
_SVG_ENTITY_RE = re.compile(rb"<!ENTITY", re.IGNORECASE)
_SVG_EXTERNAL_HREF_RE = re.compile(
    rb"""((?:xlink:)?href\s*=\s*)(["'])(?!data:|#)[^"']*\2""", re.IGNORECASE)


def sniff_vector_format(blob: bytes) -> str | None:
    """Return 'emf' / 'wmf' / 'svg' / 'pdf' / 'eps' when the bytes are one of
    those formats, else None. By magic bytes, not by the declared extension:
    python-pptx, for one, reports EMF pictures as `wmf`."""
    if not blob:
        return None
    if len(blob) >= 44 and blob[:4] == b"\x01\x00\x00\x00" and blob[40:44] == b" EMF":
        return "emf"
    if blob[:4] == b"\xd7\xcd\xc6\x9a" or _WMF_STD_RE.match(blob[:6]):
        return "wmf"
    if blob[:5] == b"%PDF-":
        return "pdf"
    if blob[:4] == b"\xc5\xd0\xd3\xc6" or blob[:4] == b"%!PS":
        return "eps"
    head = blob[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head[:4].lower() == b"<svg":
        return "svg"
    if head[:5] in (b"<?xml", b"<!--\x20", b"<!DOC") or head[:4] == b"<!--":
        if re.search(rb"<svg[\s>]", head, re.IGNORECASE):
            return "svg"
    return None


# ---------- helpers ----------

def _run_killable(cmd: list[str], timeout: float, **kw) -> subprocess.CompletedProcess:
    """subprocess.run that kills the whole process group on timeout. soffice on
    Linux is a shell script that forks soffice.bin; killing only the script
    would leave the converter running."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, **kw)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def soffice_binary() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def _content_clip(page, pymupdf):
    """Rect of the drawn content on `page` (non-white pixels of a coarse
    render), padded a little. None for a blank page."""
    from PIL import Image  # type: ignore

    rect = page.rect
    side = max(rect.width, rect.height) or 1.0
    z = min(4.0, 600.0 / side)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    bbox = img.point(lambda p: 255 if p < 245 else 0).getbbox()
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    clip = pymupdf.Rect(x0 / z, y0 / z, x1 / z, y1 / z)
    pad = max(2.0, 0.02 * max(clip.width, clip.height)) + 1.0 / z
    clip = pymupdf.Rect(clip.x0 - pad, clip.y0 - pad, clip.x1 + pad, clip.y1 + pad)
    return clip & rect


def _render_page(page, pymupdf, target_dim: int) -> tuple[bytes | None, str]:
    clip = _content_clip(page, pymupdf)
    if clip is None or clip.is_empty:
        return None, "rendered blank"
    zoom = min(MAX_ZOOM, target_dim / max(clip.width, clip.height))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
    return pix.tobytes("png"), ""


def _render_document_bytes(data: bytes, filetype: str,
                           target_dim: int) -> tuple[bytes | None, str]:
    try:
        import pymupdf  # type: ignore
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError:
            return None, "PyMuPDF is not importable (broken venv — re-run ensure_env.py)"
    try:
        doc = pymupdf.open(stream=data, filetype=filetype)
    except Exception as e:  # noqa: BLE001 — any parser error means "not renderable"
        return None, f"cannot open as {filetype}: {str(e)[:120]}"
    try:
        if doc.page_count < 1:
            return None, f"{filetype} has no pages"
        png, why = _render_page(doc[0], pymupdf, target_dim)
        if png is not None and doc.page_count > 1:
            why = f"rendered page 1 of {doc.page_count}"
        return png, why
    except Exception as e:  # noqa: BLE001
        return None, f"cannot render {filetype}: {str(e)[:120]}"
    finally:
        doc.close()


# ---------- per-format routes ----------

def _soffice_to_pdf(items: list[tuple[str, bytes, str]]) -> dict[str, bytes | str]:
    """Convert metafiles to PDF in ONE LibreOffice run. `items` are
    (key, blob, fmt); returns key → PDF bytes, or key → error string."""
    binary = soffice_binary()
    if not binary:
        return {k: LIBREOFFICE_HINT for k, _, _ in items}
    out: dict[str, bytes | str] = {}
    with tempfile.TemporaryDirectory(prefix="doc2kb-raster-") as td:
        root = Path(td)
        src, dst, profile = root / "in", root / "out", root / "profile"
        src.mkdir()
        dst.mkdir()
        names: dict[str, str] = {}
        for i, (key, blob, fmt) in enumerate(items):
            name = f"v{i:04d}"
            (src / f"{name}.{fmt}").write_bytes(blob)
            names[key] = name
        timeout = min(SOFFICE_TIMEOUT_CAP,
                      SOFFICE_TIMEOUT_BASE + SOFFICE_TIMEOUT_PER_ITEM * len(items))
        cmd = [binary, "--headless", "--norestore", "--nologo", "--nolockcheck",
               f"-env:UserInstallation={profile.as_uri()}",
               "--convert-to", "pdf", "--outdir", str(dst),
               *[str(src / f"{names[k]}.{fmt}") for k, _, fmt in items]]
        failure = ""
        try:
            proc = _run_killable(cmd, timeout)
            if proc.returncode != 0:
                tail = (proc.stderr or b"").decode("utf-8", "replace").strip()
                failure = f"LibreOffice exited {proc.returncode}: {tail[:160]}"
        except subprocess.TimeoutExpired:
            failure = f"LibreOffice timed out after {timeout}s"
        except OSError as e:
            failure = f"LibreOffice failed to start: {e}"
        for key, _, _ in items:
            pdf = dst / f"{names[key]}.pdf"
            if pdf.is_file() and pdf.stat().st_size > 0:
                out[key] = pdf.read_bytes()
            else:
                out[key] = failure or "LibreOffice could not convert it"
    return out


def _eps_previews(blob: bytes) -> tuple[bytes | None, bytes | None]:
    """(tiff, wmf) previews of a DOS-binary EPS, either may be None."""
    if blob[:4] != b"\xc5\xd0\xd3\xc6" or len(blob) < 30:
        return None, None
    _ps_off, _ps_len, wmf_off, wmf_len, tif_off, tif_len = struct.unpack("<6I", blob[4:28])

    def cut(off: int, ln: int) -> bytes | None:
        if ln and 30 <= off and off + ln <= len(blob):
            return blob[off:off + ln]
        return None
    return cut(tif_off, tif_len), cut(wmf_off, wmf_len)


def _eps_to_png(blob: bytes, target_dim: int) -> tuple[bytes | None, str]:
    gs = shutil.which("gs") or shutil.which("gswin64c")
    if gs:
        with tempfile.TemporaryDirectory(prefix="doc2kb-eps-") as td:
            src, dst = Path(td) / "in.eps", Path(td) / "out.pdf"
            src.write_bytes(blob)
            # -dSAFER: no file access outside the job (the default since gs
            # 9.50, spelled out because the input is untrusted PostScript).
            cmd = [gs, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dEPSCrop",
                   "-sDEVICE=pdfwrite", f"-sOutputFile={dst}", str(src)]
            try:
                _run_killable(cmd, GS_TIMEOUT)
            except (subprocess.TimeoutExpired, OSError):
                pass
            if dst.is_file() and dst.stat().st_size > 0:
                return _render_document_bytes(dst.read_bytes(), "pdf", target_dim)
    tiff, wmf = _eps_previews(blob)
    if tiff:
        try:
            from PIL import Image  # type: ignore
            img = Image.open(BytesIO(tiff))
            img.load()
            buf = BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            return buf.getvalue(), "EPS preview only (low resolution)" + (
                "" if gs else f"; {GHOSTSCRIPT_HINT}")
        except Exception:  # noqa: BLE001 — fall through to the WMF preview
            pass
    if wmf:
        png, why = rasterize(wmf, "wmf", target_dim=target_dim)
        if png is not None:
            return png, "EPS preview only" + ("" if gs else f"; {GHOSTSCRIPT_HINT}")
    return None, (GHOSTSCRIPT_HINT if not gs else "Ghostscript could not render it")


def _svg_sanitize(blob: bytes) -> tuple[bytes | None, str]:
    if _SVG_ENTITY_RE.search(blob):
        return None, "SVG declares XML entities — refused"
    cleaned, n = _SVG_EXTERNAL_HREF_RE.subn(rb'\1"#"', blob)
    return cleaned, (f"{n} external reference(s) dropped" if n else "")


# ---------- public API ----------

def _key(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def rasterize(blob: bytes, fmt: str, *,
              target_dim: int = RASTER_TARGET_DIM) -> tuple[bytes | None, str]:
    """Render a vector image to PNG bytes. Returns (png, detail); png is None
    when it cannot be rendered and `detail` says why (with an install hint when
    the converter is missing). Cached per process by content."""
    fmt = (sniff_vector_format(blob) or fmt or "").lower()
    if fmt not in VECTOR_FORMATS:
        return None, f"not a vector format ({fmt or 'unknown'})"
    if len(blob) > MAX_VECTOR_BYTES:
        return None, f"{fmt} image is {len(blob) // (1024 * 1024)} MiB, over the {MAX_VECTOR_BYTES // (1024 * 1024)} MiB cap"
    k = _key(blob)
    if k in _CACHE:
        return _CACHE[k]
    if fmt in ("emf", "wmf"):
        pdf = _soffice_to_pdf([(k, blob, fmt)])[k]
        res = (_render_document_bytes(pdf, "pdf", target_dim) if isinstance(pdf, bytes)
               else (None, pdf))
    elif fmt == "svg":
        cleaned, note = _svg_sanitize(blob)
        res = (None, note) if cleaned is None else _render_document_bytes(cleaned, "svg", target_dim)
        if res[0] is not None and note:
            res = (res[0], note)
    elif fmt == "pdf":
        res = _render_document_bytes(blob, "pdf", target_dim)
    else:
        res = _eps_to_png(blob, target_dim)
    _CACHE[k] = res
    return res


def prefetch(blobs: Iterable[bytes], *, target_dim: int = RASTER_TARGET_DIM) -> int:
    """Batch-convert every EMF/WMF among `blobs` in a single LibreOffice run and
    fill the cache, so the per-image `rasterize()` calls that follow are cache
    hits. Other formats render in-process and need no batching. Returns the
    number of metafiles converted (0 when soffice is absent: the per-image
    calls then report the install hint)."""
    todo: dict[str, tuple[bytes, str]] = {}
    for blob in blobs:
        if not blob or len(blob) > MAX_VECTOR_BYTES:
            continue
        fmt = sniff_vector_format(blob)
        if fmt in ("emf", "wmf"):
            k = _key(blob)
            if k not in _CACHE:
                todo[k] = (blob, fmt)
    if not todo or not soffice_binary():
        return 0
    pdfs = _soffice_to_pdf([(k, b, f) for k, (b, f) in todo.items()])
    done = 0
    for k, pdf in pdfs.items():
        if isinstance(pdf, bytes):
            _CACHE[k] = _render_document_bytes(pdf, "pdf", target_dim)
            done += _CACHE[k][0] is not None
        else:
            _CACHE[k] = (None, pdf)
    return done


def prefetch_dir(directory: Path) -> int:
    """`prefetch()` over every regular file under `directory` (pandoc's
    --extract-media tree)."""
    blobs: list[bytes] = []
    for p in sorted(Path(directory).rglob("*")):
        try:
            if p.is_file() and not p.is_symlink() and p.stat().st_size <= MAX_VECTOR_BYTES:
                with p.open("rb") as fh:
                    head = fh.read(64)
                if sniff_vector_format(head) in ("emf", "wmf"):
                    blobs.append(p.read_bytes())
        except OSError:
            continue
    return prefetch(blobs)


def prefetch_zip_media(path: Path, prefixes: tuple[str, ...] = ("word/media/", "ppt/media/", "xl/media/")) -> int:
    """`prefetch()` over the metafiles inside an OOXML package's media folders."""
    import zipfile
    blobs: list[bytes] = []
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                name = info.filename
                if not name.lower().startswith(prefixes) or info.is_dir():
                    continue
                if info.file_size > MAX_VECTOR_BYTES or info.flag_bits & 0x1:
                    continue
                if not name.lower().endswith((".emf", ".wmf", ".bin", ".tmp")):
                    continue
                with zf.open(info) as fh:
                    blobs.append(fh.read(MAX_VECTOR_BYTES + 1))
    except Exception:  # noqa: BLE001 — prefetch is an optimization only
        return 0
    return prefetch(blobs)
