#!/usr/bin/env python3
"""
_asr.py — speech-to-text worker of doc2kb's opt-in `transcribe` tier.

Runs ONLY under the tier's isolated venv (`<state>/transcribe/venv`, built by
`ensure_env.py --tier transcribe`): torch + GigaAM + silero-vad pin onnx /
onnxruntime versions that would clash with the mineru tier, so the tier never
shares the lightweight venv. Self-contained on purpose — it must not import
`_common` / `_media` (other venv). extract_media.py decodes the source with
ffmpeg and calls this script with a 16 kHz mono WAV.

Pipeline (ported from a local `transcribe` tool proven on a 30-min hall
recording where plain whisper looped through 19 of 30 minutes):
  base   whisper.cpp large-v3 on RAW audio, `-mc 0` + silero VAD — punctuated
         text; `-mc 0` (no text carry-over between windows) is what stops the
         repetition loops, VAD alone does not.
         Fallbacks when whisper-cli or its model is absent: mlx-whisper
         (Apple Silicon) → faster-whisper (CTranslate2, everywhere), both with
         condition_on_previous_text=False.
  filler GigaAM v3 e2e (Russian-only, never loops, punctuated) on an EQ'd copy
         (hum cut, presence boost, loudnorm), VAD-chunked ≤ 25 s. Used only
         when the spoken language is Russian.
  merge  base text; loop / garbage spans and silent gaps of the base are
         replaced from the filler; spans where the two disagree strongly go
         to review.md. Zero LLM calls.

Never feed whisper a prompt (it hallucinates the prompt) and never denoise
first (afftdn/DeepFilterNet removed speech on hall audio) — EQ is the fix.

CLI:
    _asr.py check  --models-dir D                       → JSON: usable engines
    _asr.py setup  --models-dir D [--all]               → fetch pinned models
    _asr.py run <wav16k> --work D --models-dir M
                [--lang auto|ru|en|…] [--engines auto|whisper,gigaam,…]
                [--no-eq] [--deadline EPOCH]
        → <work>/asr.json (+ review.md, whisper.srt/json, gigaam.txt, run.log)
          and one JSON line on stdout. Exit 2 = no usable engine (setup
          needed), 1 = failure, 0 = ok.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import warnings
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ.setdefault("PYTHONWARNINGS", "ignore")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

SR = 16000
CHUNK_MAX_S = 25.0          # GigaAM short-form hard limit
GAP_FILL_S = 6.0            # base silence ≥ this with filler text → insert filler
LOOP_FILL_MIN_S = 4.0       # shorter bad spans are dropped, not filled (avoids dup text)
DISAGREE_JACCARD = 0.35     # below → listed in review.md
LANG_DETECT_S = 30.0        # speech fed to the python engines' language detection
# whisper.cpp VAD only from this length: it guards long recordings against
# hallucinated text in silences, but on short clips (≤ a few windows, where
# loops do not happen anyway) it tipped large-v3 into lowercase, unpunctuated
# output in tests.
WHISPER_VAD_MIN_S = 120.0
EQ_CHAIN = (
    "highpass=f=110:p=2,equalizer=f=100:t=q:w=4:g=-8,"
    "equalizer=f=3000:t=q:w=1.2:g=6,highshelf=f=2000:g=8,"
    "loudnorm=I=-18:TP=-1.5:LRA=11,aresample=16000"
)

# ---- pinned model sources: HF revision + sha256 of the LFS object ----------
WHISPER_CPP_REPO = "ggerganov/whisper.cpp"
WHISPER_CPP_REV = "5359861c739e955e79d9a303bcbc70fb988958b1"
GGML_MODELS = {  # name → (bytes, sha256)
    "large-v3": (3095033483, "64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2"),
    "large-v3-turbo": (1624555275, "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69"),
    "medium": (1533763059, "6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208"),
    "small": (487601967, "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"),
    "base": (147951465, "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe"),
    "tiny": (77691713, "be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21"),
}
VAD_REPO = "ggml-org/whisper-vad"
VAD_REV = "9ffd54a1e1ee413ddf265af9913beaf518d1639b"
# v6.2.0 (same repo and revision) lost speech on lecture-hall audio with the
# flags below: whisper kept 397 / 115 of v5.1.2's 552 / 459 words on two 10-min
# lecture excerpts (GigaAM heard 862 / 714); a clean voice note was a draw.
VAD_FILE = "ggml-silero-v5.1.2.bin"
VAD_SHA = "29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf"
MLX_DEFAULT = ("mlx-community/whisper-large-v3-mlx", "49e6aa286ad60c14352c404340ded53710378a11")
FW_DEFAULT = ("Systran/faster-whisper-large-v3", "edaa852ec7e145841d8ffdb056a99866b5f0a478")
GIGAAM_DEFAULT = "v3_e2e_rnnt"
GIGAAM_FALLBACK = "v2_rnnt"
# GigaAM's own loader fetches weights by name from Sber's CDN
# (cdn.chatwm.opensmodel.sberdevices.ru; its TLS certificate expired
# 2026-11-04) into ~/.cache/gigaam. The official Hugging Face mirror keeps one
# branch per model (transformers remote-code layout); setup pins its commit,
# checks sha256 and loads the state dict into the gigaam package's own
# GigaAMASR — no transformers, no remote code, torch.load(weights_only=True).
GIGAAM_HF_REPO = "ai-sage/GigaAM-v3"
GIGAAM_HF = {  # name → (commit of the model's branch, {file: (bytes, sha256)})
    "v3_e2e_rnnt": ("7655ad717f8122257385bb4b2f373db3697e8680", {  # branch e2e_rnnt
        "config.json": (1867, "02361ba9cafd6c3ec66fcdd73494c3b562a60eb2a2d1b13f3cb04ae440d93e52"),
        "tokenizer.model": (255336, "828c12c991019eef952a960661f25a92d6ad279591e2ea466b4aeddf1d20a18a"),
        "pytorch_model.bin": (448928167, "afc6dcbae8320ea56f2cddebc0f13fbf62c9d59b6ddcad899782623c8610826a"),
    }),
}
GIGAAM_CACHE = Path("~/.cache/gigaam").expanduser()  # gigaam's default download_root

BASE_ENGINES = ("whisper", "mlx", "faster-whisper")
ALL_ENGINES = BASE_ENGINES + ("gigaam",)


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def say(msg: str) -> None:
    print(f"[doc2kb asr] {msg}", file=sys.stderr, flush=True)


@dataclass
class Seg:
    start: float
    end: float
    text: str
    src: str

    @property
    def dur(self) -> float:
        return self.end - self.start


class NeedsSetup(RuntimeError):
    """No usable engine / model — maps to exit 2 (needs_install)."""


class Deadline(RuntimeError):
    pass


def remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    left = deadline - time.time()
    if left <= 0:
        raise Deadline("deadline reached")
    return left


def ts(sec: float) -> str:
    s = max(0, int(sec))
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    return f"{h}:{m:02d}:{ss:02d}" if h else f"{m:02d}:{ss:02d}"


def norm(t: str) -> str:
    t = t.lower().replace("ё", "е")
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def wordset(t: str) -> set[str]:
    return set(norm(t).split())


def nwords(t: str) -> int:
    return len(norm(t).split())


def run(cmd: list[str], log: Path | None, deadline: float | None = None) -> subprocess.CompletedProcess:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=remaining(deadline))
    except subprocess.TimeoutExpired as e:
        raise Deadline(f"{Path(cmd[0]).name} killed at the deadline") from e
    if log is not None:
        with open(log, "a", encoding="utf-8") as f:
            f.write("$ " + " ".join(cmd) + "\n" + (p.stdout or "")[-4000:] + (p.stderr or "")[-4000:] + "\n")
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError(f"{Path(cmd[0]).name} failed ({p.returncode}): " + " | ".join(tail))
    return p


# ---------------------------------------------------------------- engines: discovery
def have(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def whisper_cpp_bin() -> str | None:
    for c in (env("DOC2KB_WHISPER_BIN"), shutil.which("whisper-cli"),
              "/opt/homebrew/bin/whisper-cli", "/usr/local/bin/whisper-cli"):
        if c and Path(c).is_file():
            return str(c)
    return None


def whisper_model_name() -> str:
    return env("DOC2KB_WHISPER_MODEL_NAME", "large-v3") or "large-v3"


def whisper_cpp_model(models_dir: Path) -> Path | None:
    p = env("DOC2KB_WHISPER_MODEL")
    if p and Path(p).expanduser().is_file():
        return Path(p).expanduser()
    cand = models_dir / f"ggml-{whisper_model_name()}.bin"
    return cand if cand.is_file() else None


def _hf_local(repo: str, rev: str | None) -> Path | None:
    if Path(repo).expanduser().is_dir():
        return Path(repo).expanduser()
    try:
        from huggingface_hub import snapshot_download
        return Path(snapshot_download(repo, revision=rev, local_files_only=True))
    except Exception:  # noqa: BLE001 — not downloaded / no hub
        return None


def mlx_source() -> tuple[str, str | None]:
    repo = env("DOC2KB_MLX_MODEL")
    return (repo, None) if repo else MLX_DEFAULT


def fw_source() -> tuple[str, str | None]:
    repo = env("DOC2KB_FW_MODEL")
    return (repo, None) if repo else FW_DEFAULT


def usable(models_dir: Path) -> dict[str, dict]:
    """Which engines can run right now (binary/module present AND weights local).
    Cheap: no torch import."""
    out: dict[str, dict] = {}
    wb, wm = whisper_cpp_bin(), whisper_cpp_model(models_dir)
    out["whisper"] = {"ok": bool(wb and wm), "bin": wb, "model": str(wm) if wm else None,
                      "vad": str(models_dir / VAD_FILE) if (models_dir / VAD_FILE).is_file() else None}
    if not wb:
        out["whisper"]["why"] = "whisper-cli not on PATH (brew install whisper-cpp)"
    elif not wm:
        out["whisper"]["why"] = f"no ggml-{whisper_model_name()}.bin — run ensure_env.py --tier transcribe"
    for name, mod, src in (("mlx", "mlx_whisper", mlx_source()),
                           ("faster-whisper", "faster_whisper", fw_source())):
        if not have(mod):
            out[name] = {"ok": False, "why": f"{mod} not installed"}
            continue
        local = _hf_local(*src)
        out[name] = {"ok": local is not None, "model": src[0], "path": str(local) if local else None}
        if local is None:
            out[name]["why"] = f"{src[0]} not downloaded — run ensure_env.py --tier transcribe"
    name = gigaam_name()
    out["gigaam"] = {"ok": have("gigaam") and have("silero_vad"), "model": name,
                     "weights": gigaam_weights(models_dir, name)}
    if not out["gigaam"]["ok"]:
        out["gigaam"]["why"] = "gigaam / silero-vad not installed"
    return out


def gigaam_name() -> str:
    return env("DOC2KB_GIGAAM_MODEL", GIGAAM_DEFAULT) or GIGAAM_DEFAULT


def gigaam_hf_dir(models_dir: Path, name: str) -> Path | None:
    """<models>/gigaam/<name> when every pinned file of the HF copy is there."""
    src = GIGAAM_HF.get(name)
    d = models_dir / "gigaam" / name
    if src and all((d / f).is_file() and (d / f).stat().st_size == size
                   for f, (size, _) in src[1].items()):
        return d
    return None


def gigaam_weights(models_dir: Path, name: str) -> str | None:
    """Where `name`'s weights would load from: 'huggingface' (the pinned copy
    in models_dir), 'gigaam-cache' (~/.cache/gigaam: the checkpoint and, for
    e2e models, the tokenizer — gigaam would fetch a missing one) or None."""
    if gigaam_hf_dir(models_dir, name):
        return "huggingface"
    need = [f"{name}.ckpt"] + ([f"{name}_tokenizer.model"] if "e2e" in name else [])
    return "gigaam-cache" if all((GIGAAM_CACHE / f).is_file() for f in need) else None


def plan(spec: str, lang: str | None, avail: dict[str, dict]) -> tuple[str, str | None, str | None]:
    """(base, filler, third) for an engine spec. `auto` = best base + GigaAM
    (the filler is dropped later if the detected language is not Russian)."""
    if spec in ("", "auto", None):
        base = next((e for e in BASE_ENGINES if avail[e]["ok"]), None)
        if base is None:
            if avail["gigaam"]["ok"] and lang == "ru":
                return "gigaam", None, None
            why = "; ".join(f"{e}: {avail[e].get('why')}" for e in BASE_ENGINES)
            raise NeedsSetup("no speech-to-text engine ready (" + why + ")")
        return base, ("gigaam" if avail["gigaam"]["ok"] and lang in (None, "ru") else None), None
    names = [x.strip() for x in spec.split(",") if x.strip()]
    bad = [n for n in names if n not in ALL_ENGINES]
    if bad:
        raise ValueError(f"unknown engine(s) {bad}; choose from {list(ALL_ENGINES)}")
    missing = [f"{n}: {avail[n].get('why')}" for n in names if not avail[n]["ok"]]
    if missing:
        raise NeedsSetup("engine(s) not ready — " + "; ".join(missing))
    if names[0] == "gigaam" and lang != "ru":
        raise ValueError("GigaAM is Russian-only — set the language to ru or list a whisper engine first")
    return names[0], (names[1] if len(names) > 1 else None), (names[2] if len(names) > 2 else None)


# ---------------------------------------------------------------- engines: running
def make_eq(raw: Path, work: Path, log: Path, deadline: float | None) -> Path:
    eq = work / "eq16k.wav"
    run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-af", EQ_CHAIN, str(eq)], log, deadline)
    return eq


def run_whisper_cpp(raw: Path, work: Path, lang: str | None, avail: dict, log: Path,
                    deadline: float | None) -> tuple[list[Seg], str | None]:
    out = work / "whisper"
    cmd = [avail["bin"], "-m", avail["model"], "-l", lang or "auto", "-f", str(raw),
           "-bs", "5", "-mc", "0", "-et", "2.0", "-np", "-osrt", "-oj", "-of", str(out)]
    if avail.get("vad") and _duration(raw) >= WHISPER_VAD_MIN_S:
        cmd += ["--vad", "-vm", avail["vad"], "-vt", "0.4", "-vsd", "300", "-vmsd", "25", "-vp", "150"]
    run(cmd, log, deadline)
    data = json.loads(out.with_suffix(".json").read_text(encoding="utf-8", errors="replace"))
    segs = []
    for x in data.get("transcription") or []:
        off = x.get("offsets") or {}
        txt = (x.get("text") or "").strip()
        if txt:
            segs.append(Seg(off.get("from", 0) / 1000, off.get("to", 0) / 1000, txt, "whisper"))
    detected = ((data.get("result") or {}).get("language") or "").strip() or None
    return segs, (lang or detected)


def _duration(wav: Path) -> float:
    try:
        import soundfile as sf
        return float(sf.info(str(wav)).duration)
    except Exception:  # noqa: BLE001 — 16-bit mono PCM: bytes → seconds
        return max(0.0, (wav.stat().st_size - 44) / (2 * SR))


def vad_chunks(wav, sr: int) -> list[tuple[int, int]]:
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad

    tsx = get_speech_timestamps(torch.from_numpy(wav), load_silero_vad(), sampling_rate=sr,
                                threshold=0.3, min_silence_duration_ms=300, speech_pad_ms=200,
                                max_speech_duration_s=CHUNK_MAX_S - 1)
    limit = int(CHUNK_MAX_S * sr)
    chunks: list[tuple[int, int]] = []
    cs = ce = None
    for t in tsx:
        s, e = t["start"], t["end"]
        while e - s > limit:  # belt and braces
            chunks.append((s, s + limit))
            s += limit
        if cs is None:
            cs, ce = s, e
        elif e - cs <= limit:
            ce = e
        else:
            chunks.append((cs, ce))
            cs, ce = s, e
    if cs is not None:
        chunks.append((cs, ce))
    return chunks


def _read_wav(path: Path):
    import soundfile as sf
    wav, sr = sf.read(str(path), dtype="float32")
    if sr != SR:
        raise RuntimeError(f"expected {SR} Hz, got {sr}")
    if getattr(wav, "ndim", 1) > 1:
        wav = wav.mean(axis=1)
    return wav


def run_mlx(raw: Path, work: Path, lang: str | None, avail: dict,
            deadline: float | None) -> tuple[list[Seg], str | None]:
    import mlx_whisper
    import numpy as np

    wav = _read_wav(raw)
    chunks = vad_chunks(wav, SR)
    model = avail["path"]
    kw = dict(path_or_hf_repo=model, condition_on_previous_text=False, temperature=0.0,
              compression_ratio_threshold=2.0, no_speech_threshold=0.5)
    if lang is None and chunks:
        sample, total = [], 0
        for s, e in chunks:
            sample.append(wav[s:e])
            total += e - s
            if total >= LANG_DETECT_S * SR:
                break
        lang = mlx_whisper.transcribe(np.concatenate(sample), **kw).get("language")
    segs: list[Seg] = []
    for s, e in chunks:
        remaining(deadline)
        r = mlx_whisper.transcribe(wav[s:e], language=lang, **kw)
        for x in r.get("segments") or []:
            if x.get("compression_ratio", 0) >= 2.0 or x.get("no_speech_prob", 0) >= 0.6:
                continue
            txt = (x.get("text") or "").strip()
            if txt:
                segs.append(Seg(s / SR + x["start"], s / SR + x["end"], txt, "mlx"))
    (work / "mlx.txt").write_text(
        "\n".join(f"[{ts(x.start)}-{ts(x.end)}] {x.text}" for x in segs) + "\n", encoding="utf-8")
    return segs, lang


def run_faster_whisper(raw: Path, work: Path, lang: str | None, avail: dict,
                       deadline: float | None) -> tuple[list[Seg], str | None]:
    from faster_whisper import WhisperModel

    device = env("DOC2KB_FW_DEVICE", "auto")
    compute = env("DOC2KB_FW_COMPUTE", "default")
    model = WhisperModel(avail["path"], device=device, compute_type=compute)
    # Hand over samples, not the path: faster-whisper's own decoder (PyAV)
    # broke on av ≥ 16 (`metadata_errors`), and the WAV is already 16 kHz mono.
    it, info = model.transcribe(_read_wav(raw), language=lang, beam_size=5, vad_filter=True,
                                condition_on_previous_text=False, temperature=0.0,
                                compression_ratio_threshold=2.4, no_speech_threshold=0.6)
    segs: list[Seg] = []
    for x in it:
        remaining(deadline)
        txt = (x.text or "").strip()
        if txt and x.compression_ratio < 2.4:
            segs.append(Seg(float(x.start), float(x.end), txt, "faster-whisper"))
    (work / "faster-whisper.txt").write_text(
        "\n".join(f"[{ts(x.start)}-{ts(x.end)}] {x.text}" for x in segs) + "\n", encoding="utf-8")
    return segs, (lang or getattr(info, "language", None))


class GigaamUnavailable(RuntimeError):
    """No GigaAM model could be loaded: not cached and not downloadable."""


def _load_gigaam_hf(d: Path, device: str):
    """GigaAMASR from the pinned HF copy in `d`: the HF config is GigaAM's own
    model cfg with `_target_: modeling_gigaam.<Class>` (the repo's remote-code
    file); point those at the gigaam package's classes, then load the state
    dict (keys under the wrapper's `model.`) strictly. Mirrors what
    gigaam.load_model does after its download."""
    import gigaam
    import torch
    from gigaam import decoder, decoding, encoder, preprocess
    from omegaconf import OmegaConf

    def retarget(node):
        if isinstance(node, dict):
            t = node.get("_target_")
            if isinstance(t, str) and t.startswith("modeling_gigaam."):
                cls = t.split(".", 1)[1]
                mod = next((m for m in (preprocess, encoder, decoder, decoding) if hasattr(m, cls)), None)
                if mod is None:
                    raise GigaamUnavailable(f"{d.name}: gigaam has no class {cls}")
                node["_target_"] = f"{mod.__name__}.{cls}"
            for v in node.values():
                retarget(v)

    cfg = json.loads((d / "config.json").read_text(encoding="utf-8"))["cfg"]["model"]["cfg"]
    retarget(cfg)
    cfg["encoder"]["flash_attn"] = False
    cfg["decoding"]["model_path"] = str(d / "tokenizer.model")
    sd = torch.load(str(d / "pytorch_model.bin"), map_location="cpu", weights_only=True)
    model = gigaam.GigaAMASR(OmegaConf.create(cfg))
    model.load_state_dict({k.removeprefix("model."): v for k, v in sd.items()})
    model = model.eval()
    dev = torch.device(device)
    if dev.type != "cpu":
        model.encoder = model.encoder.half()
    return model.to(dev)


def load_gigaam(device: str, models_dir: Path):
    """(model, name): the configured model, else GIGAAM_FALLBACK. A model with a
    pinned HF source (GIGAAM_HF) loads from its copy in models_dir (fetched by
    `setup`), else from GigaAM's own ~/.cache/gigaam — never from Sber's CDN,
    which is not revision-pinned and has been unreachable since 2026-11.
    Other models go through gigaam.load_model (cache, else CDN; md5-checked).
    Nothing loadable → GigaamUnavailable, and callers run without the filler."""
    import gigaam
    errors = []
    for name in dict.fromkeys((gigaam_name(), GIGAAM_FALLBACK)):
        where = gigaam_weights(models_dir, name)
        if name in GIGAAM_HF and where is None:
            errors.append(f"{name}: not downloaded (run `_asr.py setup`)")
            continue
        try:
            if where == "huggingface":
                return _load_gigaam_hf(gigaam_hf_dir(models_dir, name), device), name
            return gigaam.load_model(name, fp16_encoder=(device != "cpu"), device=device), name
        except (ValueError, KeyError, AssertionError, OSError, RuntimeError) as e:
            say(f"gigaam {name} unavailable ({e})")
            errors.append(f"{name}: {str(e)[:160]}")
    raise GigaamUnavailable("; ".join(errors))


def fetch_gigaam(models_dir: Path, name: str) -> str:
    """Download `name`'s pinned HF files (sha256-checked) unless GigaAM's own
    cache already has the model. Returns a status for the setup report."""
    rev, files = GIGAAM_HF[name]
    if gigaam_weights(models_dir, name) == "gigaam-cache":
        return f"cached in {GIGAAM_CACHE}"
    d = models_dir / "gigaam" / name
    d.mkdir(parents=True, exist_ok=True)
    states = [_download(f"https://huggingface.co/{GIGAAM_HF_REPO}/resolve/{rev}/{f}", d / f, sha, size)
              for f, (size, sha) in files.items()]
    return f"{GIGAAM_HF_REPO}@{rev[:10]} " + ("downloaded" if "downloaded" in states else "present")


def run_gigaam(audio: Path, work: Path, models_dir: Path,
               deadline: float | None) -> tuple[list[Seg], str]:
    import logging

    import soundfile as sf
    logging.disable(logging.WARNING)

    wav = _read_wav(audio)
    chunks = vad_chunks(wav, SR)
    model, name = load_gigaam(env("DOC2KB_GIGAAM_DEVICE", "cpu") or "cpu", models_dir)
    tmp = work / "_chunk.wav"
    segs: list[Seg] = []
    for s, e in chunks:
        remaining(deadline)
        sf.write(str(tmp), wav[s:e], SR)
        r = model.transcribe(str(tmp))
        txt = (r.text if hasattr(r, "text") else str(r)).strip()
        segs.append(Seg(s / SR, e / SR, txt, "gigaam"))
    tmp.unlink(missing_ok=True)
    (work / "gigaam.txt").write_text(
        "\n".join(f"[{ts(x.start)}-{ts(x.end)}] {x.text}" for x in segs) + "\n", encoding="utf-8")
    return segs, name


# ---------------------------------------------------------------- merge
def flag_bad(segs: list[Seg]) -> list[bool]:
    counts = Counter(norm(s.text) for s in segs if nwords(s.text) >= 4)
    bad = [False] * len(segs)
    prev = None
    for i, s in enumerate(segs):
        n = norm(s.text)
        if not n or counts.get(n, 0) >= 3 or (n == prev and nwords(n) >= 3):
            bad[i] = True
        prev = n
    # timestamp collapse: long runs of ≤1.2 s segments carrying full sentences
    run_ix: list[int] = []
    for i, s in enumerate(segs + [Seg(0, 0, "", "")]):
        if i < len(segs) and s.dur <= 1.2 and nwords(s.text) >= 4:
            run_ix.append(i)
        else:
            if len(run_ix) >= 8:
                for j in run_ix:
                    bad[j] = True
            run_ix = []
    return bad


def overlap(a: Seg, s: float, e: float) -> float:
    return max(0.0, min(a.end, e) - max(a.start, s))


def filler_for(span: tuple[float, float], filler: list[Seg], used: set[int], loose: bool) -> list[Seg]:
    s, e = span
    picked = []
    for i, f in enumerate(filler):
        if i in used or not f.text.strip():
            continue
        c = (f.start + f.end) / 2
        if overlap(f, s, e) >= 0.5 * max(f.dur, 0.1) or (loose and s <= c <= e):
            picked.append(i)
    for i in picked:
        used.add(i)
    return [filler[i] for i in picked]


def merge(base: list[Seg], filler: list[Seg], third: list[Seg] | None):
    bad = flag_bad(base)
    used: set[int] = set()
    out: list[Seg] = []
    review: list[dict] = []
    stats = {"loop_spans": 0, "loop_filled": 0, "dropped": 0, "gap_fills": 0, "disagree": 0}
    i, n = 0, len(base)
    while i < n:
        if not bad[i]:
            out.append(base[i])
            i += 1
            continue
        j = i
        while j < n and bad[j]:
            j += 1
        s, e = base[i].start, base[j - 1].end
        stats["loop_spans"] += 1
        if e - s >= LOOP_FILL_MIN_S:
            fill = filler_for((s, e), filler, used, loose=True)
            if fill:
                out.extend(fill)
                stats["loop_filled"] += 1
                review.append({"kind": "loop-fill", "s": s, "e": e,
                               "base": base[i].text[:160] + (" …" if j - i > 1 else ""),
                               "filler": " ".join(f.text for f in fill)})
            else:
                stats["dropped"] += 1
                review.append({"kind": "dropped-loop", "s": s, "e": e,
                               "base": base[i].text[:160], "filler": ""})
        else:
            stats["dropped"] += 1
        i = j
    # silent gaps in base coverage
    base_src = base[0].src if base else ""
    kept = sorted([x for x in out if x.src == base_src], key=lambda x: x.start)
    edges = [0.0] + [x.end for x in kept]
    starts = [x.start for x in kept] + [max([f.end for f in filler], default=0.0)]
    for ge, gs in zip(edges, starts):
        if gs - ge >= GAP_FILL_S:
            fill = filler_for((ge, gs), filler, used, loose=False)
            if fill:
                out.extend(fill)
                stats["gap_fills"] += 1
                review.append({"kind": "gap-fill", "s": ge, "e": gs, "base": "",
                               "filler": " ".join(f.text for f in fill)})
    out.sort(key=lambda x: (x.start, x.src != base_src))
    # disagreements: filler chunk vs base words in the same span
    for k, f in enumerate(filler):
        if k in used or nwords(f.text) < 6:
            continue
        w = [x for x in kept if overlap(x, f.start, f.end) > 0]
        if not w:
            continue
        a, b = wordset(" ".join(x.text for x in w)), wordset(f.text)
        jac = len(a & b) / max(1, len(a | b))
        if jac < DISAGREE_JACCARD:
            stats["disagree"] += 1
            entry = {"kind": f"disagreement (overlap {jac:.2f})", "s": f.start, "e": f.end,
                     "base": " ".join(x.text for x in w), "filler": f.text}
            t3 = [x.text for x in (third or []) if overlap(x, f.start, f.end) > 0.5 * max(x.dur, 0.1)]
            if t3:
                entry["third"] = " ".join(t3)
            review.append(entry)
    review.sort(key=lambda r: r["s"])
    return out, review, stats


def write_review(path: Path, title: str, review: list[dict], stats: dict,
                 base: str, filler: str | None) -> None:
    rl = [f"# {title} — spots to check", "",
          f"loop-spans {stats['loop_spans']} (filled {stats['loop_filled']}, dropped {stats['dropped']}) · "
          f"gap-fills {stats['gap_fills']} · disagreements {stats['disagree']} "
          f"(word overlap < {DISAGREE_JACCARD})", "",
          "Polish the transcript only at these spots; elsewhere the engines agree.", ""]
    for r in review:
        rl.append(f"### {ts(r['s'])}–{ts(r['e'])} · {r['kind']}")
        if r.get("base"):
            rl.append(f"- {base}: {r['base']}")
        if r.get("filler"):
            rl.append(f"- {filler}: {r['filler']}")
        if r.get("third"):
            rl.append(f"- third: {r['third']}")
        rl.append("")
    path.write_text("\n".join(rl) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- commands
def cmd_run(a: argparse.Namespace) -> int:
    t_all = time.time()
    raw = Path(a.wav).resolve()
    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    log = work / "run.log"
    log.write_text("")
    models_dir = Path(a.models_dir).expanduser()
    deadline = a.deadline
    lang = None if a.lang in (None, "", "auto") else a.lang.lower()

    avail = usable(models_dir)
    base_name, fill_name, third_name = plan(a.engines, lang, avail)
    runners = {
        "whisper": lambda lg: run_whisper_cpp(raw, work, lg, avail["whisper"], log, deadline),
        "mlx": lambda lg: run_mlx(raw, work, lg, avail["mlx"], deadline),
        "faster-whisper": lambda lg: run_faster_whisper(raw, work, lg, avail["faster-whisper"], deadline),
    }
    timings: dict[str, float] = {}
    models: dict[str, str] = {}
    notes: list[str] = []

    def timed(name, fn):
        t = time.time()
        r = fn()
        timings[name] = round(time.time() - t, 1)
        say(f"{name}: {timings[name]}s")
        return r

    results: dict[str, list[Seg]] = {}
    if base_name == "gigaam":
        audio = raw if a.no_eq else make_eq(raw, work, log, deadline)
        results["gigaam"], models["gigaam"] = timed(
            "gigaam", lambda: run_gigaam(audio, work, models_dir, deadline))
    else:
        results[base_name], lang = timed(base_name, lambda: runners[base_name](lang))
        models[base_name] = (Path(avail[base_name]["model"]).name if base_name == "whisper"
                             else avail[base_name]["model"])
    if fill_name == "gigaam" and lang not in (None, "ru"):
        notes.append(f"language {lang} — GigaAM (Russian-only) filler skipped")
        fill_name = None
    for extra in [x for x in (fill_name, third_name) if x]:
        if extra == "gigaam":
            audio = raw if a.no_eq else make_eq(raw, work, log, deadline)
            try:
                results["gigaam"], models["gigaam"] = timed(
                    "gigaam", lambda: run_gigaam(audio, work, models_dir, deadline))
            except GigaamUnavailable as e:
                notes.append(f"GigaAM filler skipped — no model could be loaded ({e})")
                fill_name = None if fill_name == "gigaam" else fill_name
                third_name = None if third_name == "gigaam" else third_name
        else:
            results[extra], _ = timed(extra, lambda: runners[extra](lang))
            models[extra] = (Path(avail[extra]["model"]).name if extra == "whisper" else avail[extra]["model"])

    base = results[base_name]
    filler = results.get(fill_name) if fill_name else []
    third = results.get(third_name) if third_name else None
    if fill_name:
        merged, review, stats = merge(base, filler or [], third)
    else:
        merged, review, stats = base, [], {"loop_spans": 0, "loop_filled": 0, "dropped": 0,
                                           "gap_fills": 0, "disagree": 0}
        bad = flag_bad(base)
        if any(bad):  # no filler to repair loops with — drop them, keep a record
            merged = [s for s, b in zip(base, bad) if not b]
            stats["loop_spans"] = stats["dropped"] = sum(
                1 for k, b in enumerate(bad) if b and (k == 0 or not bad[k - 1]))
            review = [{"kind": "dropped-loop", "s": s.start, "e": s.end, "base": s.text[:160], "filler": ""}
                      for s, b in zip(base, bad) if b][:200]
    review_path = work / "review.md"
    write_review(review_path, a.title or raw.name, review, stats, base_name, fill_name)
    if not a.keep_wav:
        for w in ("eq16k.wav", "_chunk.wav"):
            (work / w).unlink(missing_ok=True)
    payload = {
        "ok": True, "language": lang, "base": base_name, "filler": fill_name, "third": third_name,
        "models": models, "timings": timings, "elapsed": round(time.time() - t_all, 1),
        "stats": stats, "review_spots": len(review), "notes": notes,
        "words": sum(nwords(s.text) for s in merged),
        "segments": [asdict(s) for s in merged],
    }
    (work / "asr.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in payload.items() if k != "segments"}, ensure_ascii=False))
    return 0


def _download(url: str, dest: Path, sha256: str, size: int | None) -> str:
    """Resumable download into `dest` with sha256 verification. Returns a
    status word. A mismatching file is removed, never kept."""
    if dest.is_file() and (size is None or dest.stat().st_size == size):
        return "present"
    part = dest.with_name(dest.name + ".part")
    have_bytes = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "doc2kb-transcribe-setup"})
    if have_bytes:
        req.add_header("Range", f"bytes={have_bytes}-")
    shown = "" if not size else f" ({size / 1e6:,.1f} MB)" if size >= 1e6 else f" ({size / 1e3:.0f} KB)"
    say(f"downloading {dest.name}{shown} …")
    with urllib.request.urlopen(req, timeout=60) as r:
        mode = "ab" if have_bytes and r.status == 206 else "wb"
        if mode == "wb":
            have_bytes = 0
        total = (size or 0)
        done, last = have_bytes, time.time()
        with open(part, mode) as fh:
            while True:
                buf = r.read(1 << 20)
                if not buf:
                    break
                fh.write(buf)
                done += len(buf)
                if time.time() - last > 10 and total:
                    say(f"  {dest.name}: {100 * done / total:.0f}%")
                    last = time.time()
    h = hashlib.sha256()
    with open(part, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 22), b""):
            h.update(blk)
    if h.hexdigest() != sha256:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{dest.name}: sha256 mismatch — download discarded")
    os.replace(part, dest)
    return "downloaded"


def cmd_setup(a: argparse.Namespace) -> int:
    models_dir = Path(a.models_dir).expanduser()
    models_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, str] = {}
    wbin = whisper_cpp_bin()
    if wbin:
        url = f"https://huggingface.co/{VAD_REPO}/resolve/{VAD_REV}/{VAD_FILE}"
        report["whisper.cpp VAD"] = _download(url, models_dir / VAD_FILE, VAD_SHA, None)
        name = whisper_model_name()
        dest = models_dir / f"ggml-{name}.bin"
        given = env("DOC2KB_WHISPER_MODEL")
        if given and Path(given).expanduser().is_file():
            if not dest.exists():
                dest.symlink_to(Path(given).expanduser().resolve())
            report[f"ggml-{name}"] = f"linked → {Path(given).expanduser()}"
        elif name in GGML_MODELS:
            size, sha = GGML_MODELS[name]
            url = f"https://huggingface.co/{WHISPER_CPP_REPO}/resolve/{WHISPER_CPP_REV}/ggml-{name}.bin"
            report[f"ggml-{name}"] = _download(url, dest, sha, size)
        else:
            report[f"ggml-{name}"] = "unknown model name — set DOC2KB_WHISPER_MODEL to a ggml file"
    else:
        report["whisper.cpp"] = "whisper-cli not on PATH — skipped (brew install whisper-cpp for the best engine)"
    if not wbin or a.all:
        from huggingface_hub import snapshot_download
        for name, mod, src in (("mlx", "mlx_whisper", mlx_source()),
                               ("faster-whisper", "faster_whisper", fw_source())):
            if have(mod):
                say(f"fetching {src[0]} …")
                snapshot_download(src[0], revision=src[1])
                report[name] = f"{src[0]}@{(src[1] or 'main')[:10]} ready"
                if not a.all:
                    break
    if have("gigaam"):
        name, fetched = gigaam_name(), ""
        if name in GIGAAM_HF:
            try:
                fetched = fetch_gigaam(models_dir, name)
            except (OSError, RuntimeError) as e:  # URLError ⊂ OSError; sha256 mismatch
                fetched = f"download failed — {e}"
        try:
            got = load_gigaam("cpu", models_dir)[1]
            note = "" if not fetched else f" ({fetched})" if got == name else f" ({name}: {fetched})"
            report["gigaam"] = f"{got} ready{note}"
        except GigaamUnavailable as e:
            report["gigaam"] = (f"unavailable — Russian loops/gaps go unrepaired until it downloads "
                                f"(re-run `ensure_env.py --tier transcribe` later): {e}")
    avail = usable(models_dir)
    ready = any(avail[e]["ok"] for e in BASE_ENGINES)
    print(json.dumps({"ok": ready, "models_dir": str(models_dir), "setup": report,
                      "engines": {k: v["ok"] for k, v in avail.items()}}, ensure_ascii=False))
    return 0 if ready else 1


def cmd_check(a: argparse.Namespace) -> int:
    print(json.dumps(usable(Path(a.models_dir).expanduser()), ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="_asr.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "setup", "run"):
        p = sub.add_parser(name)
        p.add_argument("--models-dir", required=True)
        if name == "setup":
            p.add_argument("--all", action="store_true", help="also fetch the python engines' weights")
        if name == "run":
            p.add_argument("wav")
            p.add_argument("--work", required=True)
            p.add_argument("--lang", default="auto")
            p.add_argument("--engines", default="auto")
            p.add_argument("--title", default=None)
            p.add_argument("--no-eq", action="store_true")
            p.add_argument("--keep-wav", action="store_true")
            p.add_argument("--deadline", type=float, default=None)
    a = ap.parse_args()
    try:
        return {"check": cmd_check, "setup": cmd_setup, "run": cmd_run}[a.cmd](a)
    except NeedsSetup as e:
        print(json.dumps({"ok": False, "needs_install": True, "reason": str(e)}, ensure_ascii=False))
        return 2
    except Deadline as e:
        print(json.dumps({"ok": False, "reason": f"timeout: {e}"}, ensure_ascii=False))
        return 1
    except Exception as e:  # noqa: BLE001 — one JSON line, plainly
        print(json.dumps({"ok": False, "reason": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
