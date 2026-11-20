#!/usr/bin/env python3
"""
_media.py — shared helpers for audio / video / subtitle sources. Stdlib only:
imported by scout_corpus.py, extract_media.py and extract_subtitles.py in the
lightweight venv (the speech-to-text engines live in the isolated `transcribe`
tier and are driven through `_asr.py`).

What is here:
    * the extension sets that make a file a media / subtitle candidate;
    * `probe()` — one `ffprobe` call → a compact stream/chapter summary, and the
      rule that turns it into `audio` / `video` / not-media;
    * subtitle parsers (SRT, WebVTT, ASS/SSA, SBV, LRC) → `Cue` lists;
    * subtitle-track / sidecar choice (a translation is not a transcript);
    * `render_timed_body()` — cues → Markdown with `## <chapter | mm:ss–mm:ss>`
      sections and `[mm:ss]` paragraph marks, so search citations carry the
      time window (index_kb puts the heading path into every chunk header).

Why ffmpeg: it decodes practically every container/codec in use (≈ 400
demuxers), so "which audio/video formats are supported" reduces to "what
ffprobe can open" — the extension lists below only decide which files are
worth probing, the stream check decides.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# ---------- extensions ----------

# Unambiguous audio extensions. Everything ffmpeg reads that people actually
# keep on disk: lossy, lossless, voice-recorder, broadcast, legacy, DRM-free.
AUDIO_EXTS = frozenset({
    ".mp3", ".mp2", ".mpa", ".m4a", ".m4b", ".m4r", ".aac", ".adts", ".wav",
    ".wave", ".bwf", ".rf64", ".w64", ".flac", ".ogg", ".oga", ".opus", ".spx",
    ".wma", ".aiff", ".aif", ".aifc", ".caf", ".amr", ".awb", ".3ga", ".ac3",
    ".eac3", ".ec3", ".dts", ".dtshd", ".thd", ".mlp", ".ape", ".wv", ".tta",
    ".tak", ".mka", ".mpc", ".au", ".snd", ".ra", ".gsm", ".voc", ".qcp",
    ".dsf", ".dff", ".shn", ".ofr", ".m4p", ".weba", ".vqf", ".f4a",
})

# Unambiguous video extensions. `.ts` (TypeScript), `.mod` (go.mod), `.dat`,
# `.ps` (PostScript), `.dif`, `.sln` and `.sub` are deliberately absent: they are
# media only when libmagic says so (see scout_corpus.detect_source_type), never
# by extension alone. Raw elementary streams (.h264, .yuv, .ivf) carry no sound.
VIDEO_EXTS = frozenset({
    ".mp4", ".m4v", ".mov", ".qt", ".mkv", ".mk3d", ".webm", ".avi", ".wmv",
    ".asf", ".flv", ".f4v", ".swf", ".mpg", ".mpeg", ".mpe", ".m1v", ".m2v",
    ".mpv", ".m2p", ".vob", ".evo", ".mts", ".m2ts", ".m2t", ".trp", ".ogv",
    ".ogm", ".ogx", ".3gp", ".3g2", ".3gpp", ".dv", ".rm", ".rmvb", ".divx",
    ".xvid", ".mxf", ".nut", ".nsv", ".wtv", ".dvr-ms", ".gxf", ".lrv", ".insv",
    ".amv", ".bik", ".smk", ".roq",
})

SUBTITLE_EXTS = frozenset({".srt", ".vtt", ".webvtt", ".ass", ".ssa", ".sbv", ".lrc"})

MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS

# ffprobe "formats" that mean "a still image / text", never a recording.
_NON_MEDIA_FORMATS = {"image2", "tty", "lavfi", "gif", "apng", "webp_pipe", "svg_pipe"}
# Video codecs that are pictures (cover art, thumbnails, image sequences).
_IMAGE_CODECS = {"png", "bmp", "gif", "tiff", "webp", "jpegls", "jpeg2000", "svg", "pam",
                 "ppm", "pgm", "pbm", "pgmyuv", "targa", "sgi", "pcx", "qoi"}
# Subtitle codecs that carry text (ffmpeg can render them as SRT). Bitmap ones
# (dvd_subtitle, hdmv_pgs_subtitle, dvb_subtitle, xsub) need OCR.
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "microdvd",
                   "subviewer", "subviewer1", "sami", "realtext", "jacosub", "mpl2", "pjs",
                   "stl", "vplayer", "ttml", "arib_caption"}
BITMAP_SUB_CODECS = {"dvd_subtitle", "hdmv_pgs_subtitle", "dvb_subtitle", "xsub",
                     "dvb_teletext"}

PROBE_TIMEOUT_SEC = 60


def ffprobe_bin() -> str | None:
    return shutil.which("ffprobe")


def ffmpeg_bin() -> str | None:
    return shutil.which("ffmpeg")


FFMPEG_INSTALL_HINT = (
    "ffmpeg/ffprobe not on PATH — install ffmpeg (`brew install ffmpeg` / "
    "`apt install ffmpeg` / `winget install ffmpeg`) and re-run extract_corpus.py"
)


# ---------- language tags ----------

_LANG3_TO_2 = {
    "rus": "ru", "eng": "en", "ukr": "uk", "bel": "be", "kaz": "kk", "ger": "de",
    "deu": "de", "fre": "fr", "fra": "fr", "spa": "es", "ita": "it", "por": "pt",
    "pol": "pl", "cze": "cs", "ces": "cs", "slo": "sk", "slk": "sk", "chi": "zh",
    "zho": "zh", "jpn": "ja", "kor": "ko", "ara": "ar", "heb": "he", "tur": "tr",
    "dut": "nl", "nld": "nl", "swe": "sv", "nor": "no", "nob": "no", "dan": "da",
    "fin": "fi", "gre": "el", "ell": "el", "hun": "hu", "rum": "ro", "ron": "ro",
    "bul": "bg", "srp": "sr", "hrv": "hr", "slv": "sl", "lit": "lt", "lav": "lv",
    "est": "et", "geo": "ka", "kat": "ka", "arm": "hy", "hye": "hy", "aze": "az",
    "uzb": "uz", "per": "fa", "fas": "fa", "hin": "hi", "vie": "vi", "tha": "th",
    "ind": "id", "may": "ms", "msa": "ms", "cat": "ca", "baq": "eu", "eus": "eu",
}
_KNOWN_LANG2 = frozenset(_LANG3_TO_2.values())
_LANG_NAMES = {"russian": "ru", "english": "en", "ukrainian": "uk", "german": "de",
               "french": "fr", "spanish": "es", "italian": "it", "chinese": "zh",
               "japanese": "ja", "korean": "ko", "portuguese": "pt", "polish": "pl"}


def norm_lang(tag: str | None) -> str | None:
    """ISO 639-1/2 tag, region suffix or English name → 2-letter code; None
    for unknown (`und`, empty, garbage)."""
    if not tag:
        return None
    t = str(tag).strip().lower().replace("_", "-")
    if not t or t in {"und", "unk", "mul", "zxx", "mis", "auto"}:
        return None
    t = t.split("-")[0]
    if t in _LANG_NAMES:
        return _LANG_NAMES[t]
    if len(t) == 2 and t.isalpha():
        return t
    if len(t) == 3 and t.isalpha():
        return _LANG3_TO_2.get(t, t)
    return None


def sidecar_lang(sub_path: Path, media_stem: str) -> str | None:
    """`lecture.ru.srt` / `lecture.rus.vtt` / `lecture.en-US.srt` → language of
    a sidecar subtitle file named after `media_stem`; None when unlabeled."""
    rest = sub_path.stem[len(media_stem):].strip(" ._-")
    if not rest:
        return None
    for part in reversed(re.split(r"[._ ]+", rest)):
        low = part.lower()
        if low in _LANG_NAMES:
            return _LANG_NAMES[low]
        head = low.split("-")[0]
        if (len(head) == 2 and head in _KNOWN_LANG2) or head in _LANG3_TO_2:
            return norm_lang(head)
    return None


# ---------- probe ----------

def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x >= 0 else None  # NaN / negative → unknown


def probe(path: Path, timeout: int = PROBE_TIMEOUT_SEC) -> dict[str, Any]:
    """Summarize a media file with one ffprobe call.

    Returns {"ok": True, "format", "duration", "title", "audio": [...],
    "video": [...], "subs_text": [...], "subs_bitmap": [...], "chapters": [...]}
    or {"ok": False, "reason": ..., "needs_install": bool}. `video` excludes
    cover art (`attached_pic`) and still-image codecs, so an MP3 with album art
    is audio and a PNG is not media at all."""
    exe = ffprobe_bin()
    if not exe:
        return {"ok": False, "reason": FFMPEG_INSTALL_HINT, "needs_install": True}
    cmd = [exe, "-v", "error", "-print_format", "json", "-show_format",
           "-show_streams", "-show_chapters", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": f"ffprobe timed out after {timeout}s", "needs_install": False}
    except OSError as e:
        return {"ok": False, "reason": f"ffprobe failed: {e}", "needs_install": False}
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        data = {}
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    if proc.returncode != 0 or not streams:
        err = proc.stderr.decode("utf-8", "replace").strip().replace(str(path), path.name).splitlines()
        return {"ok": False, "needs_install": False,
                "reason": "ffprobe found no streams" + (f": {err[-1][:200]}" if err else "")}
    fmt_name = str(fmt.get("format_name") or "")
    out: dict[str, Any] = {
        "ok": True, "format": fmt_name,
        "format_long": fmt.get("format_long_name"),
        "duration": _f(fmt.get("duration")),
        "bit_rate": int(_f(fmt.get("bit_rate")) or 0) or None,
        "title": ((fmt.get("tags") or {}).get("title")
                  or (fmt.get("tags") or {}).get("TITLE")),
        "audio": [], "video": [], "subs_text": [], "subs_bitmap": [], "chapters": [],
    }
    image_format = fmt_name in _NON_MEDIA_FORMATS or fmt_name.endswith("_pipe")
    max_stream_dur = 0.0
    for s in streams:
        kind = s.get("codec_type")
        codec = str(s.get("codec_name") or "")
        tags = s.get("tags") or {}
        disp = s.get("disposition") or {}
        base = {
            "index": s.get("index"),
            "codec": codec,
            "language": norm_lang(tags.get("language")),
            "title": tags.get("title"),
            "default": bool(disp.get("default")),
        }
        sd = _f(s.get("duration"))
        if sd:
            max_stream_dur = max(max_stream_dur, sd)
        if kind == "audio":
            base.update(channels=s.get("channels"), sample_rate=s.get("sample_rate"))
            out["audio"].append(base)
        elif kind == "video":
            if disp.get("attached_pic") or codec in _IMAGE_CODECS or image_format:
                continue
            base.update(width=s.get("width"), height=s.get("height"))
            out["video"].append(base)
        elif kind == "subtitle":
            base["forced"] = bool(disp.get("forced"))
            if codec in TEXT_SUB_CODECS:
                out["subs_text"].append(base)
            elif codec in BITMAP_SUB_CODECS:
                out["subs_bitmap"].append(base)
    if out["duration"] is None and max_stream_dur:
        out["duration"] = max_stream_dur
    for ch in data.get("chapters") or []:
        st, en = _f(ch.get("start_time")), _f(ch.get("end_time"))
        if st is None:
            continue
        title = ((ch.get("tags") or {}).get("title") or "").strip()
        out["chapters"].append({"start": st, "end": en, "title": title})
    return out


def media_kind(info: dict[str, Any]) -> str | None:
    """`video` when a real picture stream exists, `audio` when only sound,
    None when the probe found neither (an image, a text file, garbage)."""
    if not info.get("ok"):
        return None
    if info["video"]:
        return "video"
    if info["audio"]:
        return "audio"
    return None


# ---------- time formatting ----------

def fmt_ts(sec: float, long: bool = False) -> str:
    """`mm:ss`, or `h:mm:ss` when `long` (recordings of an hour or more)."""
    s = max(0, int(sec))
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    if long:
        return f"{h}:{m:02d}:{ss:02d}"
    return f"{h * 60 + m:02d}:{ss:02d}"


# ---------- subtitles ----------

@dataclass
class Cue:
    start: float
    end: float
    text: str
    src: str = ""

    @property
    def dur(self) -> float:
        return self.end - self.start


_TS_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:[.,](\d{1,3}))?")
_ARROW_RE = re.compile(r"^\s*(\S+)\s*-->\s*(\S+)")
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_VTT_TS_TAG_RE = re.compile(r"<\d+:\d{2}(?::\d{2})?[.,]\d+>")
_VTT_VOICE_RE = re.compile(r"<v(?:\.[^ >]*)?\s+([^>]+)>")
_ASS_OVERRIDE_RE = re.compile(r"\{[^}]*\}")


def parse_ts(s: str) -> float | None:
    m = _TS_RE.fullmatch(s.strip())
    if not m:
        return None
    h, mi, se, frac = m.groups()
    val = int(h or 0) * 3600 + int(mi) * 60 + int(se)
    if frac:
        val += int(frac) / (10 ** len(frac))
    return float(val)


def _clean_line(line: str) -> str:
    line = _VTT_TS_TAG_RE.sub("", line)
    line = _VTT_VOICE_RE.sub(lambda m: m.group(1).strip() + ": ", line)
    line = _HTML_TAG_RE.sub("", line)
    line = _ASS_OVERRIDE_RE.sub("", line)
    line = line.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return " ".join(line.split())


def _arrow_cues(text: str) -> list[Cue]:
    """SRT and WebVTT share the shape: optional id line, `a --> b [settings]`,
    text lines, blank line."""
    cues: list[Cue] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i, n = 0, len(lines)
    while i < n:
        m = _ARROW_RE.match(lines[i])
        if not m:
            i += 1
            continue
        st, en = parse_ts(m.group(1)), parse_ts(m.group(2))
        i += 1
        buf: list[str] = []
        while i < n and lines[i].strip():
            if _ARROW_RE.match(lines[i]):
                break
            buf.append(lines[i])
            i += 1
        # Skip a trailing numeric id that belongs to the next SRT block.
        if buf and buf[-1].strip().isdigit() and i < n and _ARROW_RE.match(lines[i]):
            buf.pop()
        if st is None:
            continue
        txt = [c for c in (_clean_line(b) for b in buf) if c]
        if txt:
            cues.append(Cue(st, en if en is not None else st, "\n".join(txt)))
    return cues


def _ass_cues(text: str) -> list[Cue]:
    cues: list[Cue] = []
    fields: list[str] | None = None
    in_events = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_events = line.lower() == "[events]"
            continue
        if not in_events:
            continue
        if line.lower().startswith("format:"):
            fields = [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
            continue
        if not line.lower().startswith("dialogue:"):
            continue
        fl = fields or ["layer", "start", "end", "style", "name", "marginl", "marginr",
                        "marginv", "effect", "text"]
        parts = line.split(":", 1)[1].split(",", len(fl) - 1)
        if len(parts) < len(fl):
            continue
        row = dict(zip(fl, (p.strip() for p in parts)))
        body = row.get("text", "")
        if re.search(r"\{[^}]*\\p[1-9]", body):  # vector drawing, not text
            continue
        body = body.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
        txt = [c for c in (_clean_line(b) for b in body.split("\n")) if c]
        st, en = parse_ts(row.get("start", "")), parse_ts(row.get("end", ""))
        if txt and st is not None:
            cues.append(Cue(st, en if en is not None else st, "\n".join(txt)))
    cues.sort(key=lambda c: c.start)
    return cues


def _sbv_cues(text: str) -> list[Cue]:
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = block.split("\n")
        if not lines or "," not in lines[0]:
            continue
        a, _, b = lines[0].partition(",")
        st, en = parse_ts(a), parse_ts(b)
        txt = [c for c in (_clean_line(x) for x in lines[1:]) if c]
        if st is not None and txt:
            cues.append(Cue(st, en if en is not None else st, "\n".join(txt)))
    return cues


_LRC_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")


def _lrc_cues(text: str) -> list[Cue]:
    cues: list[Cue] = []
    for line in text.splitlines():
        stamps = list(_LRC_RE.finditer(line))
        if not stamps:
            continue
        lyric = _clean_line(_LRC_RE.sub("", line))
        if not lyric:
            continue
        for m in stamps:
            mi, se, frac = m.groups()
            st = int(mi) * 60 + int(se) + (int(frac) / 10 ** len(frac) if frac else 0)
            cues.append(Cue(float(st), float(st), lyric))
    cues.sort(key=lambda c: c.start)
    for a, b in zip(cues, cues[1:]):
        a.end = b.start
    return cues


def subtitle_format(path: Path, text: str) -> str:
    ext = path.suffix.lower().lstrip(".")
    if ext in {"ass", "ssa"} or "[Events]" in text[:20000]:
        return "ass"
    if ext in {"vtt", "webvtt"} or text.lstrip("﻿").startswith("WEBVTT"):
        return "vtt"
    if ext == "sbv":
        return "sbv"
    if ext == "lrc":
        return "lrc"
    return "srt"


def dedupe_rolling(cues: list[Cue]) -> list[Cue]:
    """Drop lines a cue repeats from the previous one — only when the track
    rolls. Auto-generated captions (YouTube VTT) re-show the previous line in
    every cue, so the plain concatenation would duplicate most of the text. A
    normal track (< 30 % of cues opening with the previous cue's last line)
    is returned untouched: a genuinely repeated «Да.» stays."""
    rolling = sum(1 for a, b in zip(cues, cues[1:])
                  if b.text.split("\n")[0] == a.text.split("\n")[-1])
    if len(cues) < 4 or rolling < 0.3 * (len(cues) - 1):
        return cues
    out: list[Cue] = []
    prev_lines: list[str] = []
    for c in cues:
        lines = c.text.split("\n")
        k = 0
        while k < len(lines) and prev_lines and lines[k] in prev_lines[-2:]:
            k += 1
        new = lines[k:]
        prev_lines = lines
        if not new:
            if out:
                out[-1].end = max(out[-1].end, c.end)
            continue
        out.append(Cue(c.start, c.end, "\n".join(new), c.src))
    return out


def parse_subtitles(text: str, fmt: str) -> list[Cue]:
    text = text.lstrip("﻿")
    if fmt == "ass":
        cues = _ass_cues(text)
    elif fmt == "sbv":
        cues = _sbv_cues(text)
    elif fmt == "lrc":
        cues = _lrc_cues(text)
    else:
        cues = _arrow_cues(text)
    return dedupe_rolling(cues)


@dataclass
class SubChoice:
    kind: str                 # "sidecar" | "track"
    ref: Any                  # Path for a sidecar, stream index for a track
    lang: str | None
    label: str
    default: bool = False
    forced: bool = False
    extra: dict = field(default_factory=dict)


def _sub_rank(c: SubChoice) -> tuple[int, int, int]:
    return (0 if c.default else 1, 0 if c.kind == "sidecar" else 1, 0 if c.lang else 1)


def choose_subtitles(cands: list[SubChoice], audio_lang: str | None,
                     want_lang: str | None) -> tuple[SubChoice | None, list[SubChoice], str]:
    """Pick the subtitle source that is a transcript of the speech.

    Returns (choice, fallbacks, why). Forced tracks (foreign-dialogue-only)
    never count. With a known target language (forced, else the audio
    stream's tag) a subtitle in another language is a translation → no
    choice, why="translation". Without one, subtitles in two or more
    languages are ambiguous (yt-dlp leaves `video.en.vtt` + `video.ru.vtt`) →
    no choice, why="ambiguous". `fallbacks` are what the caller may still use
    when speech-to-text is unavailable, best first."""
    usable = sorted((c for c in cands if not c.forced), key=_sub_rank)
    if not usable:
        return None, [], "none"
    target = want_lang or audio_lang
    if target:
        same = [c for c in usable if c.lang == target]
        unknown = [c for c in usable if c.lang is None]
        other = [c for c in usable if c.lang is not None and c.lang != target]
        pool = same or unknown
        if not pool:
            return None, other, "translation"
        return pool[0], other, "match"
    langs = {c.lang for c in usable if c.lang}
    if len(langs) > 1:
        return None, usable, "ambiguous"
    return usable[0], [], "match"


# ---------- body rendering ----------

PARA_GAP_S = 2.5          # silence that starts a new paragraph
PARA_MAX_CHARS = 500
SECTION_WINDOW_S = 300    # `## mm:ss–mm:ss` every 5 min when there are no chapters


def _sections(duration: float, chapters: list[dict], long: bool) -> list[tuple[float, str]]:
    chs = [c for c in chapters if c.get("start") is not None]
    if len(chs) >= 2:
        out = []
        for k, c in enumerate(chs):
            end = c.get("end")
            if end is None:
                end = chs[k + 1]["start"] if k + 1 < len(chs) else duration
            span = f"{fmt_ts(c['start'], long)}–{fmt_ts(end or c['start'], long)}"
            title = " ".join((c.get("title") or "").split())
            out.append((c["start"], f"{title} ({span})" if title else f"Chapter {k + 1} ({span})"))
        return out
    if duration <= SECTION_WINDOW_S * 1.2:
        return []
    out = []
    t = 0.0
    while t < duration:
        end = min(t + SECTION_WINDOW_S, duration)
        out.append((t, f"{fmt_ts(t, long)}–{fmt_ts(end, long)}"))
        t += SECTION_WINDOW_S
    return out


def render_timed_body(title: str | None, cues: Iterable[Cue], *, duration: float | None,
                      chapters: list[dict] | None = None, note: str | None = None,
                      base_src: str = "", level: int = 2) -> tuple[str, list[str]]:
    """Cues → (markdown body, section headings).

    Paragraph = consecutive cues of one source with < 2.5 s silence, ≤ 500
    chars, inside one section; it starts with `[mm:ss]` (or `[h:mm:ss]`), and
    with `⟨src⟩` when the text came from a filler engine (`src != base_src`).
    Sections are the file's chapters (≥ 2), else 5-minute windows (only for
    recordings longer than ~6 min)."""
    cues = [c for c in cues if c.text.strip()]
    dur = duration or max((c.end for c in cues), default=0.0)
    long = dur >= 3600
    secs = _sections(dur, chapters or [], long)
    starts = [s for s, _ in secs]

    def sec_of(t: float) -> int:
        k = -1
        for i, s in enumerate(starts):
            if t + 1e-6 >= s:
                k = i
        return k

    lines: list[str] = [f"# {title}", ""] if title else []
    if note:
        lines += [f"> {note}", ""]
    cur: list[Cue] = []
    cur_sec = -2
    emitted_sec = -2

    def flush() -> None:
        nonlocal emitted_sec
        if not cur:
            return
        s = sec_of(cur[0].start)
        if s != emitted_sec and s >= 0:
            lines.extend([f"{'#' * level} {secs[s][1]}", ""])
        emitted_sec = s
        tag = f" ⟨{cur[0].src}⟩" if cur[0].src and cur[0].src != base_src else ""
        text = " ".join(" ".join(c.text.split()) for c in cur)
        lines.extend([f"[{fmt_ts(cur[0].start, long)}]{tag} {text}", ""])

    for c in cues:
        s = sec_of(c.start)
        if cur and (c.src != cur[-1].src or c.start - cur[-1].end > PARA_GAP_S
                    or sum(len(x.text) for x in cur) > PARA_MAX_CHARS or s != cur_sec):
            flush()
            cur = []
        if not cur:
            cur_sec = s
        cur.append(c)
    flush()
    return "\n".join(lines).rstrip() + "\n", [h for _, h in secs]
