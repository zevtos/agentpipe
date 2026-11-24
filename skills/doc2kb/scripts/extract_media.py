#!/usr/bin/env python3
"""
extract_media.py — Phase 4 extractor for audio and video files.

CLI:
    extract_media.py <input_media> <output_md>
                     [--doc-id doc-NNN] [--source-rel rel/path/in/corpus.mp4]
                     [--lang auto|ru|en|…]          (env DOC2KB_TRANSCRIBE_LANG, default auto)
                     [--engines auto|whisper,gigaam,mlx,faster-whisper]
                                                     (env DOC2KB_ASR_ENGINES, default auto)
                     [--subtitles prefer|ignore]     (env DOC2KB_MEDIA_SUBTITLES, default prefer)
                     [--sidecar <path.srt>]…         subtitle files that belong to this file
                     [--no-eq]                       feed GigaAM raw audio (clean studio sound)

Effect:
    Writes <output_md> (YAML frontmatter + Markdown) and prints one JSON line.
    Body: `# <file name>`, a one-line note on how the text was produced, then
    `## <chapter | mm:ss–mm:ss>` sections of `[mm:ss] …` paragraphs, so every
    search hit cites the time window it came from.

Where the text comes from (first that applies):
    1. Subtitles that transcribe the speech — a sidecar file (`lecture.ru.srt`,
       passed by extract_corpus from scout's pairing) or a text subtitle track
       inside the container (SRT/ASS/WebVTT/mov_text …, rendered by ffmpeg).
       A track whose language differs from the audio's is a translation and is
       skipped unless nothing else can produce text. Forced tracks never count.
    2. Speech-to-text through the opt-in `transcribe` tier (`_asr.py` in its
       own venv): whisper.cpp large-v3 `-mc 0` + VAD as the punctuated base,
       GigaAM v3 filling whisper's loops and silent gaps when the speech is
       Russian (both run side by side); disputed spans →
       `<kb_dir>/_media/<doc_id>/review.md`. $DOC2KB_ASR_PROFILE picks the
       pipeline: `quality` (that pair), `fast` (one engine — GigaAM alone for
       Russian speech, the whisper base alone otherwise), `auto` (default:
       quality with a GPU, fast on a CPU-only host, where whisper large-v3
       runs near real time and GigaAM 40× faster).

Exit codes: 0 ok · 1 failure (unreadable media, no audio and no text, engine
crash, timeout) · 2 needs install (ffmpeg missing, or the transcribe tier not
installed while the file needs speech-to-text) — extract_corpus surfaces 2 as
`needs_install`, never as an error.

Timeouts: extract_corpus gives a media job `max(--timeout, 2 × duration +
15 min)` and passes the deadline in $DOC2KB_EMBED_DEADLINE; the worker runs in
its own process group and is killed with it before that deadline, so no
whisper-cli is left orphaned.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import ensure_env  # isolated tier paths (stdlib-only module)
from _common import (  # noqa: E402
    detect_text_encoding,
    emit_failure,
    emit_success,
    sanitize_heading,
    sha256_of,
    today_iso,
    tool_version_string,
    validate_source_rel,
    write_md,
)
from _embedded import ENV_DEADLINE  # noqa: E402
from _media import (  # noqa: E402
    FFMPEG_INSTALL_HINT,
    Cue,
    SubChoice,
    choose_subtitles,
    ffmpeg_bin,
    media_kind,
    norm_lang,
    parse_subtitles,
    probe,
    render_timed_body,
    sidecar_lang,
    subtitle_format,
)

TIER = "transcribe"
SCRIPTS_DIR = Path(__file__).resolve().parent
ASR_SCRIPT = SCRIPTS_DIR / "_asr.py"
TIER_HINT = (
    "speech-to-text tier not installed — run `python3 <skill_dir>/scripts/ensure_env.py "
    "--tier transcribe` (≈1 GB deps + up to 3.5 GB pinned models; `brew install "
    "whisper-cpp` first gives the best engine), then re-run extract_corpus.py"
)
DECODE_TIMEOUT_SEC = 1800
ENGINE_LABEL = {"whisper": "whisper.cpp", "mlx": "mlx-whisper", "faster-whisper": "faster-whisper",
                "gigaam": "GigaAM"}
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _deadline() -> float | None:
    try:
        return float(os.environ[ENV_DEADLINE])
    except (KeyError, ValueError):
        return None


def _left(deadline: float | None, margin: float = 0.0) -> float | None:
    if deadline is None:
        return None
    return max(1.0, deadline - time.time() - margin)


def _run_group(cmd: list[str], timeout: float | None) -> tuple[int | str, str, str]:
    """Run `cmd` in its own process group and kill the whole group on timeout
    (the ASR worker starts whisper-cli; killing only the worker would orphan
    it). Returns (returncode | "timeout", stdout, stderr)."""
    kw: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kw)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        out, err = proc.communicate()
        return "timeout", out or "", err or ""


def _last_json(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _pick_audio(streams: list[dict], want: str | None) -> dict | None:
    if not streams:
        return None
    if want:
        for s in streams:
            if s.get("language") == want:
                return s
    return next((s for s in streams if s.get("default")), streams[0])


def _read_sidecar(path: Path) -> list[Cue]:
    enc = detect_text_encoding(path, candidates=("cp1251", "koi8-r", "cp1252", "iso-8859-1"),
                               validator=lambda t: "�" not in t)
    text = path.read_bytes().decode(enc, errors="replace")
    return parse_subtitles(text, subtitle_format(path, text))


def _read_track(src: Path, index: int, timeout: float | None) -> list[Cue]:
    rc, out, err = _run_group([ffmpeg_bin() or "ffmpeg", "-v", "error", "-nostdin", "-i", str(src),
                               "-map", f"0:{index}", "-f", "srt", "-"], timeout)
    if rc != 0:
        raise RuntimeError(f"ffmpeg could not render subtitle stream {index}: {err.strip()[-200:]}")
    return parse_subtitles(out, "srt")


def _sub_candidates(info: dict, sidecars: list[Path], media_stem: str) -> list[SubChoice]:
    cands: list[SubChoice] = []
    for p in sidecars:
        lang = sidecar_lang(p, media_stem)
        cands.append(SubChoice("sidecar", p, lang, f"sidecar {p.name}"))
    for s in info["subs_text"]:
        bits = [s["codec"]] + [x for x in (s.get("language"), s.get("title")) if x]
        cands.append(SubChoice("track", s["index"], s.get("language"),
                               f"stream {s['index']} ({', '.join(bits)})",
                               default=s.get("default", False), forced=s.get("forced", False)))
    return cands


def _transcribe(src: Path, audio: dict, work: Path, args, title: str,
                deadline: float | None) -> dict:
    """Decode the chosen audio stream to 16 kHz mono and run the tier worker.
    Returns the worker's asr.json payload; raises RuntimeError / _NeedsInstall."""
    work.mkdir(parents=True, exist_ok=True)
    wav = work / "raw16k.wav"
    rc, _, err = _run_group([ffmpeg_bin() or "ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(src),
                             "-map", f"0:{audio['index']}", "-vn", "-ac", "1", "-ar", "16000",
                             "-c:a", "pcm_s16le", str(wav)],
                            min(DECODE_TIMEOUT_SEC, _left(deadline, 30) or DECODE_TIMEOUT_SEC))
    if rc != 0 or not wav.is_file():
        raise RuntimeError(f"ffmpeg could not decode audio stream {audio['index']}: "
                           f"{(err or str(rc)).strip()[-300:]}")
    cmd = [str(ensure_env.isolated_python(TIER)), str(ASR_SCRIPT), "run", str(wav),
           "--work", str(work), "--models-dir", str(ensure_env.isolated_models_dir(TIER)),
           "--lang", args.lang, "--engines", args.engines, "--title", title]
    if args.no_eq:
        cmd.append("--no-eq")
    if deadline is not None:
        cmd += ["--deadline", f"{deadline - 10:.3f}"]
    try:
        rc, out, err = _run_group(cmd, _left(deadline, 5))
    finally:
        if not os.environ.get("DOC2KB_MEDIA_KEEP_WAV"):
            wav.unlink(missing_ok=True)
    res = _last_json(out) or {}
    if rc == "timeout":
        raise RuntimeError("speech-to-text timed out (raise extract_corpus --timeout or "
                           "DOC2KB_MEDIA_TIMEOUT_FACTOR)")
    if rc == 2:
        raise _NeedsInstall(res.get("reason") or TIER_HINT)
    if rc != 0 or not res.get("ok"):
        tail = err.strip().splitlines()[-3:]
        raise RuntimeError(res.get("reason") or f"asr worker exit {rc}: {' | '.join(tail)}")
    return json.loads((work / "asr.json").read_text(encoding="utf-8"))


class _NeedsInstall(RuntimeError):
    pass


def extract(in_path: Path, out_path: Path, doc_id: str, source_rel: str, args) -> tuple[str, dict, list[str], str]:
    """Returns (body, frontmatter extras, warnings, extraction_method).
    Raises _NeedsInstall (exit 2) or RuntimeError (exit 1)."""
    info = probe(in_path)
    if not info.get("ok"):
        if info.get("needs_install"):
            raise _NeedsInstall(FFMPEG_INSTALL_HINT)
        raise RuntimeError(f"not a readable media file: {info.get('reason')}")
    if not ffmpeg_bin():
        raise _NeedsInstall(FFMPEG_INSTALL_HINT)
    kind = media_kind(info) or ("video" if info["subs_text"] else None)
    if kind is None:
        raise RuntimeError("ffprobe found no audio, video or text subtitle stream")

    warnings: list[str] = []
    name = Path(source_rel).name
    want = norm_lang(args.lang)
    audio = _pick_audio(info["audio"], want)
    audio_lang = (audio or {}).get("language")
    deadline = _deadline()
    work = out_path.parent.parent / "_media" / _SAFE_ID_RE.sub("_", doc_id)

    if info["subs_bitmap"]:
        codecs = sorted({s["codec"] for s in info["subs_bitmap"]})
        warnings.append(f"media: {len(info['subs_bitmap'])} bitmap subtitle track(s) "
                        f"({', '.join(codecs)}) not extracted — image subtitles need OCR")

    sidecars = [Path(p) for p in (args.sidecar or []) if Path(p).is_file()]
    cands = _sub_candidates(info, sidecars, in_path.stem)
    choice, fallbacks, why = (None, [], "ignored")
    if args.subtitles != "ignore":
        choice, fallbacks, why = choose_subtitles(cands, audio_lang, want)
    tier_ok = ensure_env.isolated_ready(TIER)
    if choice is None and fallbacks and (not audio or not tier_ok):
        choice = fallbacks[0]
        if why == "translation":
            warnings.append(
                f"media: subtitles in {choice.lang} used as the text — the audio is "
                f"{audio_lang or want}"
                + ("" if not audio else "; install the transcribe tier for a transcript of the speech"))
        else:
            langs = ", ".join(sorted({c.lang for c in fallbacks if c.lang}))
            warnings.append(
                f"media: subtitles in several languages ({langs}) and the spoken language is "
                f"unknown — used {choice.label}; set DOC2KB_TRANSCRIBE_LANG or a per-file "
                "media.lang to pick, or install the transcribe tier to transcribe the speech")

    extras: dict = {}
    cues: list[Cue] = []
    method = ""
    note = ""
    base_src = ""
    if choice is not None:
        try:
            cues = (_read_sidecar(choice.ref) if choice.kind == "sidecar"
                    else _read_track(in_path, choice.ref, _left(deadline, 30)))
        except (OSError, RuntimeError) as e:
            warnings.append(f"media: subtitles unreadable ({choice.label}): {e}")
            cues = []
        if cues:
            extras.update(transcript_source="subtitles", subtitle_track=choice.label)
            method = ("subtitles-sidecar" if choice.kind == "sidecar" else "ffmpeg-subtitles")
            note = "Text of the subtitles; [mm:ss] = start of the cue."
            if choice.lang:
                extras["language"] = choice.lang
        elif choice.kind == "track":
            warnings.append(f"media: subtitle {choice.label} has no text — falling back to speech")

    if not cues:
        if not audio:
            raise RuntimeError("no audio stream and no usable text subtitles — nothing to extract")
        if not tier_ok:
            raise _NeedsInstall(TIER_HINT)
        res = _transcribe(in_path, audio, work, args, name, deadline)
        base_src = res["base"]
        cues = [Cue(s["start"], s["end"], s["text"], s["src"]) for s in res["segments"]]
        engines = [e for e in (res["base"], res.get("filler"), res.get("third")) if e]
        models = res.get("models") or {}
        labels = [f"{ENGINE_LABEL.get(e, e)}({str(models.get(e, '')).removesuffix('.bin')})" for e in engines]
        method = "ffmpeg+" + "+".join(labels)
        lang = res.get("language")
        review_rel = (work / "review.md").relative_to(out_path.parent.parent).as_posix()
        # Kept short and generic: every word here is indexed with the body
        # (engine/model names live in frontmatter `extraction_method`).
        note = ("Machine transcript of the speech — names and terms may be misheard. "
                "[mm:ss] = start of a passage"
                + (f"; ⟨{res['filler']}⟩ = text from the second engine" if res.get("filler") else "")
                + ".")
        st = res.get("stats") or {}
        extras.update(transcript_source="asr", asr_engines=engines, asr_words=res.get("words", 0),
                      asr_seconds=res.get("elapsed"))
        if lang:
            extras["language"] = lang
        if res.get("review_spots"):
            extras.update(asr_review=review_rel, asr_review_spots=res["review_spots"])
            warnings.append(f"media: {res['review_spots']} spot(s) where the engines disagree or "
                            f"filled a loop/gap — optional polish, see {review_rel}")
        if st.get("dropped") and not res.get("filler"):
            warnings.append(f"media: {st['dropped']} looped span(s) dropped with no second engine "
                            "to refill them — possible gaps")
        if not cues:
            warnings.append("media: no speech recognized (music, silence or a language the "
                            "engines do not know)")

    chapters = info.get("chapters") or []
    body, _sections = render_timed_body(name, cues, duration=info.get("duration"),
                                        chapters=chapters, note=note, base_src=base_src)
    # Paired sidecar files are corpus files whose own entry scout set to skip:
    # whatever did not become the main text is appended, never dropped.
    used = choice.ref if (choice is not None and choice.kind == "sidecar"
                          and extras.get("transcript_source") == "subtitles") else None
    appended: list[str] = []
    for c in cands:
        if c.kind != "sidecar" or c.ref == used:
            continue
        try:
            sub_cues = _read_sidecar(c.ref)
        except OSError as e:
            warnings.append(f"media: sidecar {c.ref.name} unreadable: {e}")
            continue
        if not sub_cues:
            continue
        sub_body, _ = render_timed_body(None, sub_cues, duration=None, level=3)
        body = (body.rstrip() + f"\n\n## Subtitles: {c.ref.name}"
                + (f" ({c.lang})" if c.lang else "") + "\n\n" + sub_body)
        appended.append(c.ref.name)
    if appended:
        extras["sidecars_appended"] = appended
    if len(chapters) >= 2:
        extras["headings"] = [h for h in (sanitize_heading(c.get("title") or "") for c in chapters) if h][:50]
        extras["chapters"] = len(chapters)
    dur = info.get("duration")
    if dur:
        # Seconds only: an unquoted `mm:ss` scalar reads as a base-60 int in YAML 1.1.
        extras["duration_seconds"] = int(round(dur))
    extras["source_type"] = kind
    extras["media_format"] = info.get("format")
    if audio:
        extras["audio_codec"] = audio.get("codec")
    if info["video"]:
        v = info["video"][0]
        extras["video_codec"] = v.get("codec")
        if v.get("width") and v.get("height"):
            extras["resolution"] = f"{v['width']}x{v['height']}"
    if len(info["audio"]) > 1:
        extras["audio_streams"] = len(info["audio"])
    if cands:
        extras["subtitle_sources"] = len(cands)
    if info.get("title"):
        extras["media_title"] = sanitize_heading(str(info["title"]))
    return body, extras, warnings, method or "ffmpeg"


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract audio/video → timed Markdown "
                                             "(subtitles, else speech-to-text).")
    ap.add_argument("input_media")
    ap.add_argument("output_md")
    ap.add_argument("--doc-id", default="doc-000")
    ap.add_argument("--source-rel", default=None)
    ap.add_argument("--lang", default=os.environ.get("DOC2KB_TRANSCRIBE_LANG") or "auto")
    ap.add_argument("--engines", default=os.environ.get("DOC2KB_ASR_ENGINES") or "auto")
    ap.add_argument("--subtitles", choices=["prefer", "ignore"],
                    default=os.environ.get("DOC2KB_MEDIA_SUBTITLES") or "prefer")
    ap.add_argument("--sidecar", action="append", default=[],
                    help="subtitle file belonging to this media file (repeatable)")
    ap.add_argument("--no-eq", action="store_true")
    args = ap.parse_args()

    in_path = Path(args.input_media).expanduser().resolve()
    out_path = Path(args.output_md).expanduser().resolve()
    try:
        source_rel = validate_source_rel(args.source_rel or in_path.name)
    except ValueError as e:
        emit_failure(f"invalid --source-rel: {e}")
        return 1
    if not in_path.is_file():
        emit_failure(f"input not found: {in_path}")
        return 1
    if args.subtitles not in ("prefer", "ignore"):
        args.subtitles = "prefer"

    try:
        body, extras, warnings, method = extract(in_path, out_path, args.doc_id, source_rel, args)
    except _NeedsInstall as e:
        emit_failure(str(e), extra={"input": str(in_path), "needs_install": True})
        return 2
    except Exception as e:  # noqa: BLE001 — one JSON line, plainly
        emit_failure(f"extraction failed: {e}", extra={"input": str(in_path)})
        return 1

    from _common import count_tokens  # noqa: E402  (tiktoken import is slow; only on success)
    kind = extras.pop("source_type")
    fm = {
        "id": args.doc_id,
        "source": source_rel,
        "source_type": kind,
        "source_sha256": sha256_of(in_path),
        "extraction_method": f"{method}@{tool_version_string()}",
        "extraction_date": today_iso(),
        "headings": extras.pop("headings", []),
        **extras,
        "tokens_estimated": count_tokens(body),
        "warnings": warnings,
    }
    write_md(out_path, fm, body)
    emit_success(out_path, body, warnings, extra={
        "extractor": method, "source_type": kind,
        "transcript_source": extras.get("transcript_source"),
        "duration_seconds": extras.get("duration_seconds"),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
