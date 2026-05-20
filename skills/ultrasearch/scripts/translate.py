#!/usr/bin/env python3
"""
translate.py — multilingual query + abstract translation (Stage 3).

Stage 3 scope (research §11 line 229; 03_stage3.md §3.4):
    - `argos-translate` for offline EN↔RU translation (no API key, no network)
    - `langdetect` for query language detection
    - Two flows:
        1. Query expansion: non-English query → EN parallel query for English
           sources (OpenAlex, S2, arXiv); used by discover.py via `discover(...,
           translate=True)`.
        2. Abstract translation: RU candidate → EN translation cached in
           papers.abstract_translated (Stage 3 schema column).

argos-translate is pip-installable, downloads ~50 MB per direction (en→ru,
ru→en) on first call. Cached at ~/.local/share/argos-translate/.

CLI:
    translate.py --text "Hello world" --to ru
    translate.py --detect "нейроинтерфейсы P300"
    translate.py --abstracts --query-lang ru   # translate untranslated RU abstracts in corpus
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Literal, TypedDict

from _common import emit_failure, get_env, iso_now, json_stdout, log


SUPPORTED_LANGS = frozenset({"en", "ru"})
DEFAULT_LANG = "en"
ABSTRACT_TRANSLATE_BATCH = 50


class TranslationResult(TypedDict, total=False):
    ok: bool
    src_lang: str
    dst_lang: str
    text: str
    elapsed_s: float
    error: str


class DetectResult(TypedDict, total=False):
    ok: bool
    lang: str
    confidence: float


# ---------- argos-translate lazy import ----------

_ARGOS_INSTALLED_PAIRS: set[tuple[str, str]] = set()


def _ensure_argos_pair(src: str, dst: str) -> tuple[bool, str | None]:
    """Install the argos language pair (src→dst) on first call.
    Returns (ok, error_msg). Cached at module level so repeat calls no-op."""
    if (src, dst) in _ARGOS_INSTALLED_PAIRS:
        return True, None
    try:
        import argostranslate.package as pkg  # type: ignore
        import argostranslate.translate as trans  # type: ignore
    except ImportError as e:
        return False, f"argos-translate not importable: {e}"
    try:
        # Find installed languages first
        installed = trans.get_installed_languages()
        names = {(lang.code, t.to_lang.code)
                 for lang in installed
                 for t in lang.translations_from}
        if (src, dst) in names:
            _ARGOS_INSTALLED_PAIRS.add((src, dst))
            return True, None
        # Need to download the package
        log(f"argos: downloading {src}→{dst} language pack (~50MB)",
            prefix="translate")
        pkg.update_package_index()
        available = pkg.get_available_packages()
        candidate = next((p for p in available
                          if p.from_code == src and p.to_code == dst), None)
        if candidate is None:
            return False, f"no argos pkg for {src}→{dst}"
        download_path = candidate.download()
        pkg.install_from_path(download_path)
        _ARGOS_INSTALLED_PAIRS.add((src, dst))
        return True, None
    except Exception as e:
        return False, f"argos install failed: {e}"


# ---------- detect ----------

def detect_lang(text: str) -> DetectResult:
    """langdetect ISO 639-1 code. Maps unsupported langs to 'en'."""
    if not text or not text.strip():
        return {"ok": False, "lang": DEFAULT_LANG, "confidence": 0.0}
    try:
        from langdetect import detect_langs, DetectorFactory  # type: ignore
        # Force deterministic output across runs — without this, the same
        # query may detect differently from one invocation to the next.
        DetectorFactory.seed = 0
    except ImportError as e:
        log(f"langdetect not importable: {e}", prefix="translate")
        # heuristic fallback: Cyrillic ratio
        cyr = sum(1 for c in text if "Ѐ" <= c <= "ӿ")
        if cyr / max(len(text), 1) > 0.3:
            return {"ok": True, "lang": "ru", "confidence": 0.7}
        return {"ok": True, "lang": "en", "confidence": 0.5}
    try:
        results = detect_langs(text)
        if not results:
            return {"ok": False, "lang": DEFAULT_LANG, "confidence": 0.0}
        top = results[0]
        return {"ok": True, "lang": top.lang, "confidence": float(top.prob)}
    except Exception as e:
        log(f"langdetect error: {e}", prefix="translate")
        return {"ok": False, "lang": DEFAULT_LANG, "confidence": 0.0}


# ---------- translate ----------

def translate_text(text: str, *, src: str | None = None,
                   dst: str = "en") -> TranslationResult:
    """Translate `text` from `src` (auto-detected if None) to `dst`.
    Stage 3 supports en↔ru only — other pairs return ok=False."""
    if not text or not text.strip():
        return {"ok": True, "src_lang": src or "und", "dst_lang": dst, "text": text}
    t0 = time.monotonic()
    if src is None:
        det = detect_lang(text)
        src = det.get("lang", DEFAULT_LANG)
    if src == dst:
        return {"ok": True, "src_lang": src, "dst_lang": dst, "text": text,
                "elapsed_s": round(time.monotonic() - t0, 3)}
    if src not in SUPPORTED_LANGS or dst not in SUPPORTED_LANGS:
        return {"ok": False, "src_lang": src, "dst_lang": dst,
                "error": f"unsupported_pair_{src}_{dst}",
                "elapsed_s": round(time.monotonic() - t0, 3)}
    ok, err = _ensure_argos_pair(src, dst)
    if not ok:
        return {"ok": False, "src_lang": src, "dst_lang": dst,
                "error": err or "argos_install_failed",
                "elapsed_s": round(time.monotonic() - t0, 3)}
    try:
        import argostranslate.translate as trans  # type: ignore
        out = trans.translate(text, src, dst)
        return {"ok": True, "src_lang": src, "dst_lang": dst, "text": out,
                "elapsed_s": round(time.monotonic() - t0, 3)}
    except Exception as e:
        return {"ok": False, "src_lang": src, "dst_lang": dst,
                "error": f"translate_failed: {e}",
                "elapsed_s": round(time.monotonic() - t0, 3)}


# ---------- query expansion ----------

def expand_query(query: str, *, target_lang: str = "en") -> dict[str, str]:
    """Return {lang: translation} pairs. For an RU query, returns
    {'ru': original, 'en': translated}. Used by discover.py when --lang auto
    is on and source language is not English."""
    det = detect_lang(query)
    src = det.get("lang", DEFAULT_LANG)
    out: dict[str, str] = {src: query}
    if src != target_lang and target_lang in SUPPORTED_LANGS:
        r = translate_text(query, src=src, dst=target_lang)
        if r.get("ok"):
            out[target_lang] = r["text"]
    return out


# ---------- abstract batch translation ----------

def translate_abstracts(con: sqlite3.Connection, *,
                        query_lang: str = "ru",
                        target_lang: str = "en",
                        batch_size: int = ABSTRACT_TRANSLATE_BATCH
                        ) -> dict[str, Any]:
    """Find papers whose abstract is in `query_lang` and `abstract_translated`
    is NULL; translate and persist."""
    rows = con.execute(
        "SELECT paper_id, abstract FROM papers "
        "WHERE abstract IS NOT NULL AND abstract <> '' "
        "  AND abstract_translated IS NULL"
    ).fetchall()
    candidates: list[tuple[str, str]] = []
    for r in rows:
        det = detect_lang(r["abstract"])
        if det.get("lang") == query_lang:
            candidates.append((r["paper_id"], r["abstract"]))
    if not candidates:
        return {"ok": True, "n_translated": 0, "n_skipped": 0}

    n_translated = 0
    n_failed = 0
    for batch_start in range(0, len(candidates), batch_size):
        batch = candidates[batch_start : batch_start + batch_size]
        # Translate OUTSIDE the writer lock — each argos call takes seconds
        # and would otherwise block every other corpus writer for the whole
        # batch (reviewer medium finding).
        translated: list[tuple[str, str]] = []
        for paper_id, text in batch:
            r = translate_text(text, src=query_lang, dst=target_lang)
            if not r.get("ok"):
                n_failed += 1
                continue
            translated.append((paper_id, r["text"]))

        # Bulk UPDATE inside a single short writer_tx.
        if translated:
            con.execute("BEGIN IMMEDIATE")
            try:
                for paper_id, text in translated:
                    con.execute(
                        "UPDATE papers SET abstract_translated = ? "
                        "WHERE paper_id = ?",
                        (text, paper_id),
                    )
                    n_translated += 1
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
    return {"ok": True, "n_translated": n_translated, "n_failed": n_failed,
            "n_candidates": len(candidates)}


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(prog="translate.py")
    parser.add_argument("--text", default=None, help="text to translate")
    parser.add_argument("--src", default=None, help="source lang (auto-detect if omitted)")
    parser.add_argument("--to", default="en", help="target lang code (default: en)")
    parser.add_argument("--detect", default=None, help="just detect language of arg, no translation")
    parser.add_argument("--abstracts", action="store_true",
                        help="batch-translate untranslated abstracts in corpus.db")
    parser.add_argument("--query-lang", default="ru",
                        help="source lang for --abstracts (default: ru)")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    if args.detect:
        r = detect_lang(args.detect)
        json_stdout(r)
        return 0
    if args.text:
        r = translate_text(args.text, src=args.src, dst=args.to)
        json_stdout(r)
        return 0 if r.get("ok") else 1
    if args.abstracts:
        from index import open_corpus
        try:
            con = open_corpus(Path(args.db) if args.db else None)
        except RuntimeError as e:
            emit_failure(str(e))
            return 1
        try:
            r = translate_abstracts(con, query_lang=args.query_lang)
        finally:
            con.close()
        json_stdout(r)
        return 0

    emit_failure("provide --text, --detect, or --abstracts")
    return 2


if __name__ == "__main__":
    sys.exit(main())
