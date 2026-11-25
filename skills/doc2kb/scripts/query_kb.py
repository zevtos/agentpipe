#!/usr/bin/env python3
"""
query_kb.py — the doc2kb search engine: ranked passages with file:line refs.

`index_kb.py` builds `<kb_dir>/_index.db` (SQLite FTS5 over structure-aware,
heading-path-headed chunks); this file searches it and answers the way an agent
needs: the best passages IN FULL, each headed by where it sits —

    [1] docs/doc-007-virtualbox.md:13066-13082 · Fine Tuning the NAT Engine · p.266
    In NAT mode, the guest network interface is assigned to the IPv4 range …

— so one call is usually the whole lookup, and when it is not, `Read` with that
line range is the next step (no second grep to find the place).

Design constraints (so a SECOND agent / human can run it anywhere):
  * **Pure stdlib.** Only `sqlite3` + the standard library — no venv, no
    third-party deps, no network. `index_kb.py` copies this file verbatim into
    `<kb_dir>/_query.py` so the KB is self-contained and portable: any machine
    with `python3` can search it via `./query.sh "<question>"`.
  * **Never stale.** That copy is as old as the KB, so before answering it
    looks for an installed doc2kb with a higher ENGINE_REV and hands the call
    over (`_delegate`). The engine then checks the index against the docs and
    its own format (`ensure_fresh`) and re-indexes first when it has to. An
    upgrade of the skill or an edited note reaches every KB on the next query.
  * **Morphology in Python.** FTS5's built-in tokenizers stem English only
    (`porter`), and a custom FTS5 tokenizer needs SQLite's C API, which the
    stdlib `sqlite3` module does not expose. So this file carries the analyzer
    — fold → tokenize → Snowball Russian / Snowball English stemmer — and
    `index_kb.py` imports it: index-time and query-time analysis are the same
    function. "затратами на качество" matches "затраты на качество".
  * **Exact tokens.** The tokenizer splits `10.0.2.15` and `--mac-address` on
    their punctuation, so the query side turns such a token into a phrase
    (`parse_query`): the parts must be adjacent, in order.
  * **FTS5 with an identical fallback.** FTS5 indexes the analyzer's output
    with its `ascii` tokenizer, which reproduces those tokens exactly. If this
    interpreter's SQLite lacks FTS5, the chunks are analyzed on the fly and a
    pure-Python BM25 scores them with FTS5's own `bm25()` formula (k1=1.2,
    b=0.75, same IDF, same column weights), so both backends return the same
    ranking — the fallback is just slower (~0.5 s per 1M tokens).
  * **BM25 sign.** FTS5 `bm25()` returns *more negative = more relevant*; we
    `ORDER BY bm25(...) ASC` and report a flipped (positive, higher-is-better)
    score.
  * **Several KBs at once.** The first argument may be a KB or any folder above
    KBs; hits from different KBs are ranked by `fit` (score as a share of the
    best possible for the query in that KB), which is comparable where raw
    BM25 is not.
  * **Old indexes still work.** An `_index.db` from an older format that cannot
    be rebuilt here (the bundled copy travels without the indexer) is still
    searched — without line refs (format 2) or stemming (format 1) — and the
    output says so.

CLI (`USAGE` below has every option):
    query_kb.py <kb_dir | folder with KBs> "<question>" ["<question>" ...]
    query_kb.py <folder> --list | --info

Importable: `run_search()`, `resolve_kbs()`, `render_list()` — what `kb_mcp.py`
(the MCP server) and `dkb query` call.

Exit codes: 0 ok (even with zero hits), 2 usage/db error.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

INDEX_DB_NAME = "_index.db"

# Revision of this file's behaviour. A KB's bundled copy (`_query.py`) yields to
# an installed copy with a HIGHER number, and the engine refreshes a KB whose
# bundled copy is lower — bump it on every change an older copy should not keep
# answering for.
ENGINE_REV = 3

# Format of `_index.db` this engine reads natively (index_kb.py writes it). An
# index of another format is rebuilt before the query when index_kb is at hand.
INDEX_SCHEMA = "3"

# Identifier of analyze() below. index_kb.py records it in `meta.analyzer` and
# folds it into the corpus signature, so ANY change to folding, tokenizing or
# stemming must bump it — existing indexes then rebuild instead of silently
# mismatching the query side.
ANALYZER = "snowball-ru-en-1"

# Minimal EN+RU stoplist — dropped from OR queries so ranking is driven by the
# content terms, not by "the/of/и/в". Kept deliberately small; BM25's IDF already
# down-weights ubiquitous terms, this just trims obvious noise from the MATCH.
# Matched against surface forms, before stemming.
_STOPWORDS = frozenset("""
a an and are as at be by for from has he in is it its of on that the to was were
will with what which who whom whose why how when where can could should would do
does did not no yes or but if then than this these those there here their our your
и в во не на я с со как а то все она так его но да ты к у же вы за бы по только ее
мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни
быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они тут
где есть надо ней для мы тебя их чем была сам чтоб без чего раз тоже себе под будет
""".split())

# Token = run of ASCII digits/letters, Latin-1/Extended-A/B letters (× and ÷
# excluded) or Cyrillic letters (U+0482–U+0489 signs/combining marks excluded).
_TOKEN_RE = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ɏЀ-ҁҊ-ӿ]+")


# ---------- folding ----------

class _FoldTable(dict):
    """str.translate table, filled lazily per code point (C-speed after warm-up).

    * A Latin character whose NFKD starts with ASCII loses its diacritics
      (`é`→`e`, ligature `ﬁ`→`fi`) — mirrors FTS5 unicode61 remove_diacritics.
    * Any other character maps to itself: Cyrillic is NEVER decomposed — `й`
      would become `и`+breve and `ё` `е`+diaeresis, corrupting the token.
    * A standalone combining mark is dropped. After NFC that is only what has
      no precomposed form — notably the stress accent in Russian textbooks
      (`зна́ние` → `знание`), which would otherwise split the word in two.
    """

    def __missing__(self, cp: int) -> str:
        ch = chr(cp)
        if unicodedata.combining(ch):
            v = ""
        else:
            d = unicodedata.normalize("NFKD", ch)
            if d != ch and d[:1].isascii():
                # .lower(): compatibility forms can expand to capitals (№ → No, ™ → TM)
                v = "".join(c for c in d if not unicodedata.combining(c)).lower()
            else:
                v = ch
        self[cp] = v
        return v


_FOLD = _FoldTable()


def _fold(s: str) -> str:
    """NFC + lowercase + Latin-only diacritic folding (see _FoldTable)."""
    return unicodedata.normalize("NFC", s).lower().translate(_FOLD)


def _tokenize(text: str) -> list[str]:
    """Surface tokens (folded, unstemmed). Also the analyzer of pre-stemming
    (legacy) indexes."""
    return _TOKEN_RE.findall(_fold(text))


# ---------- Snowball stemmers ----------
#
# Pure-Python ports of the Snowball "russian" and "english" (Porter2)
# algorithms, transcribed from the code that Snowball 3.1.1 generates
# (russian.sbl / english.sbl, PyPI `snowballstemmer` 3.1.1). Verified
# word-for-word against snowballstemmer 3.1.1: 0 mismatches on 113k Russian
# (a university corpus + synthetic inflections) and 250k English words. The
# English apostrophe rules are omitted: the tokenizer never yields one.
#
# Snowball license (BSD-3-Clause), retained as its clause 1 requires:
#
#   Copyright (c) 2001, Dr Martin Porter
#   Copyright (c) 2004,2005, Richard Boulton
#   Copyright (c) 2013, Yoshiki Shibukawa
#   Copyright (c) 2006-2025, Olly Betts
#   All rights reserved.
#
#   Redistribution and use in source and binary forms, with or without
#   modification, are permitted provided that the following conditions
#   are met:
#
#     1. Redistributions of source code must retain the above copyright notice,
#        this list of conditions and the following disclaimer.
#     2. Redistributions in binary form must reproduce the above copyright
#        notice, this list of conditions and the following disclaimer in the
#        documentation and/or other materials provided with the distribution.
#     3. Neither the name of the Snowball project nor the names of its
#        contributors may be used to endorse or promote products derived from
#        this software without specific prior written permission.
#
#   THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#   AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#   IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#   ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
#   LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#   CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#   SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#   INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#   CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#   ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#   POSSIBILITY OF SUCH DAMAGE.

def _longest(w: str, table: Any, maxlen: int, floor: int = 0) -> Optional[str]:
    """Longest suffix of w that is in `table` and starts at index >= floor
    (Snowball `among` matches the longest entry, never a shorter fallback)."""
    for n in range(min(maxlen, len(w) - floor), 0, -1):
        s = w[-n:]
        if s in table:
            return s
    return None


_RU_V = frozenset("аеиоуыэюя")
# value 1 = the suffix must follow а/я (which stays); 2 = delete unconditionally
_RU_GERUND = {"в": 1, "вши": 1, "вшись": 1,
              "ив": 2, "ыв": 2, "ивши": 2, "ывши": 2, "ившись": 2, "ывшись": 2}
_RU_ADJECTIVE = frozenset(
    "ее ие ое ые ими ыми ей ий ой ый ем им ом ым его ого ему ому их ых ею ою ую юю ая яя"
    .split())
_RU_PARTICIPLE = {"ем": 1, "нн": 1, "вш": 1, "щ": 1, "ющ": 1, "ивш": 2, "ывш": 2, "ующ": 2}
_RU_REFLEXIVE = frozenset(("ся", "сь"))
_RU_VERB = {
    **dict.fromkeys("ла на ете йте ли й л ем н ло но нно ет ют ны ть ешь".split(), 1),
    **dict.fromkeys("ила ыла ена ейте уйте ите или ыли ей уй ил ыл им ым ен ило ыло "
                    "ено ует уют ит ыт ят ены ить ыть ишь ю ую".split(), 2),
}
_RU_NOUN = frozenset(
    "а ев ов е ие ье и еи ии ами ями иями й ей ией ий ой ам ем ием ом ям иям о у ах ях "
    "иях ы ь ю ию ью я ия ья".split())
_RU_DERIVATIONAL = frozenset(("ост", "ость"))
_RU_TIDY = frozenset(("ейше", "ейш", "н", "ь"))


def _ru_conditional(w: str, rv: int, table: dict, maxlen: int) -> Optional[str]:
    s = _longest(w, table, maxlen, rv)
    if s is None:
        return None
    i = len(w) - len(s)
    if table[s] == 1 and not (i > rv and w[i - 1] in "ая"):
        return None
    return w[:i]


def stem_ru(word: str) -> str:
    """Snowball Russian stemmer. Input: lowercase Cyrillic word."""
    w = word.replace("ё", "е")
    n = len(w)
    rv = p2 = n
    i = 0
    while i < n and w[i] not in _RU_V:
        i += 1
    if i < n:
        rv = i = i + 1
        while i < n and w[i] in _RU_V:
            i += 1
        i += 1
        while i < n and w[i] not in _RU_V:
            i += 1
        i += 1
        while i < n and w[i] in _RU_V:
            i += 1
        if i < n:
            p2 = i + 1
    if rv >= n:
        return w

    # Step 1: perfective gerund, else (reflexive?) adjectival | verb | noun
    r = _ru_conditional(w, rv, _RU_GERUND, 6)
    if r is not None:
        w = r
    else:
        if _longest(w, _RU_REFLEXIVE, 2, rv):
            w = w[:-2]
        s = _longest(w, _RU_ADJECTIVE, 3, rv)
        if s:
            w = w[:-len(s)]
            r = _ru_conditional(w, rv, _RU_PARTICIPLE, 3)
            if r is not None:
                w = r
        else:
            r = _ru_conditional(w, rv, _RU_VERB, 4)
            if r is not None:
                w = r
            else:
                s = _longest(w, _RU_NOUN, 4, rv)
                if s:
                    w = w[:-len(s)]
    # Step 2: и
    if len(w) > rv and w[-1] == "и":
        w = w[:-1]
    # Step 3: derivational ост(ь), in R2
    s = _longest(w, _RU_DERIVATIONAL, 4, rv)
    if s and len(w) - len(s) >= p2:
        w = w[:-len(s)]
    # Step 4: tidy up — superlative ейш(е), нн → н, soft sign
    s = _longest(w, _RU_TIDY, 4, rv)
    if s == "ь":
        w = w[:-1]
    elif s:
        if s != "н":
            w = w[:-len(s)]
        if len(w) - 2 >= rv and w.endswith("нн"):
            w = w[:-1]
    return w


_EN_V = frozenset("aeiouy")
_EN_EXCEPTIONS = {
    "skis": "ski", "skies": "sky", "idly": "idl", "gently": "gentl", "ugly": "ugli",
    "early": "earli", "only": "onli", "singly": "singl",
    "sky": "sky", "news": "news", "howe": "howe", "atlas": "atlas", "cosmos": "cosmos",
    "bias": "bias", "andes": "andes",
}
_EN_R1_PREFIXES = ("arsen", "commun", "emerg", "gener", "inter", "later", "organ",
                   "past", "univers")
_EN_STEP1B = (("eedly", 1), ("ingly", 2), ("edly", 2), ("eed", 1), ("ing", 3), ("ed", 2))
_EN_STEP2 = {
    "tional": "tion", "enci": "ence", "anci": "ance", "abli": "able", "entli": "ent",
    "izer": "ize", "ization": "ize", "ational": "ate", "ation": "ate", "ator": "ate",
    "alli": "al", "aliti": "al", "alism": "al", "fulli": "ful", "fulness": "ful",
    "ousli": "ous", "ousness": "ous", "iveness": "ive", "iviti": "ive",
    "biliti": "ble", "bli": "ble", "ogist": "og", "ogi": None, "lessli": "less",
    "li": None,
}
_EN_STEP3 = {"tional": "tion", "ational": "ate", "alize": "al", "icate": "ic",
             "iciti": "ic", "ical": "ic", "ful": "", "ness": "", "ative": None}
_EN_STEP4 = frozenset("ic ance ence able ible ate ive ize iti al ism er ous ant ent "
                      "ment ement ion".split())


def _en_shortv(w: str) -> bool:
    n = len(w)
    if n >= 3 and w[-1] not in "aeiouywxY" and w[-2] in _EN_V and w[-3] not in _EN_V:
        return True
    if n == 2 and w[-1] not in _EN_V and w[-2] in _EN_V:
        return True
    return w.endswith("past")


def _en_step1b(w: str, p1: int) -> str:
    for suf, grp in _EN_STEP1B:
        if w.endswith(suf):
            break
    else:
        return w
    i = len(w) - len(suf)
    pre = w[:i]
    if grp == 1:                                   # eed, eedly
        if i >= p1 and pre not in ("succ", "proc", "exc"):
            return pre + "ee"
        return w
    if grp == 3:                                   # ing
        for e in ("even", "cann", "earr", "herr", "inn", "out", "y"):
            if pre.endswith(e):
                if e == "y":
                    if len(pre) == 2 and pre[0] not in _EN_V:
                        return pre[0] + "ie"       # dying → die
                elif len(pre) == len(e):
                    return w                       # inning, outing, evening ...
                break
    if not any(c in _EN_V for c in pre):           # ed/edly/ingly/ing: need a vowel
        return w
    w = pre
    if w.endswith(("at", "bl", "iz")):
        return w + "e"
    if w.endswith(("bb", "dd", "ff", "gg", "mm", "nn", "pp", "rr", "tt")):
        if len(w) == 3 and w[0] in "aeo":
            return w
        return w[:-1]
    if len(w) == p1 and _en_shortv(w):
        return w + "e"
    return w


def stem_en(word: str) -> str:
    """Snowball English (Porter2) stemmer. Input: lowercase ASCII letters."""
    exc = _EN_EXCEPTIONS.get(word)
    if exc is not None:
        return exc
    if len(word) < 3:
        return word
    w = list(word)
    y_found = False
    if w[0] == "y":
        w[0] = "Y"
        y_found = True
    for i in range(1, len(w)):
        if w[i] == "y" and w[i - 1] in _EN_V:
            w[i] = "Y"
            y_found = True
    w = "".join(w)
    n = len(w)

    p1 = p2 = n
    i = next((len(p) for p in _EN_R1_PREFIXES if w.startswith(p)), -1)
    if i < 0:
        i = 0
        while i < n and w[i] not in _EN_V:
            i += 1
        i += 1
        while i < n and w[i] in _EN_V:
            i += 1
        i += 1
    if i <= n:
        p1 = i
        while i < n and w[i] not in _EN_V:
            i += 1
        i += 1
        while i < n and w[i] in _EN_V:
            i += 1
        if i < n:
            p2 = i + 1

    # Step 1a
    if w.endswith("sses"):
        w = w[:-2]
    elif w.endswith(("ied", "ies")):
        w = w[:-3] + ("i" if len(w) > 4 else "ie")
    elif w.endswith(("ss", "us")):
        pass
    elif w.endswith("s") and any(c in _EN_V for c in w[:-2]):
        w = w[:-1]
    # Step 1b
    w = _en_step1b(w, p1)
    # Step 1c
    if len(w) >= 3 and w[-1] in "yY" and w[-2] not in _EN_V:
        w = w[:-1] + "i"
    # Step 2
    s = _longest(w, _EN_STEP2, 7)
    if s and len(w) - len(s) >= p1:
        i = len(w) - len(s)
        rep = _EN_STEP2[s]
        if rep is not None:
            w = w[:i] + rep
        elif s == "ogi":
            if i > 0 and w[i - 1] == "l":
                w = w[:i] + "og"
        elif i > 0 and w[i - 1] in "cdeghkmnrt":   # li
            w = w[:i]
    # Step 3
    s = _longest(w, _EN_STEP3, 7)
    if s and len(w) - len(s) >= p1:
        i = len(w) - len(s)
        rep = _EN_STEP3[s]
        if rep is not None:
            w = w[:i] + rep
        elif i >= p2:                              # ative
            w = w[:i]
    # Step 4
    s = _longest(w, _EN_STEP4, 5)
    if s and len(w) - len(s) >= p2:
        i = len(w) - len(s)
        if s != "ion" or (i > 0 and w[i - 1] in "st"):
            w = w[:i]
    # Step 5
    if w.endswith("e"):
        i = len(w) - 1
        if i >= p2 or (i >= p1 and not _en_shortv(w[:-1])):
            w = w[:-1]
    elif w.endswith("l"):
        i = len(w) - 1
        if i >= p2 and i > 0 and w[i - 1] == "l":
            w = w[:-1]
    return w.replace("Y", "y") if y_found else w


# ---------- analyzer (shared with index_kb.py) ----------

_RU_WORD_RE = re.compile(r"[а-яё]+")
_STEM_CACHE: dict[str, str] = {}


def stem_token(tok: str) -> str:
    """Stem one folded token: Russian for [а-яё]+, English for [a-z]+, anything
    else (digits, mixed scripts, other Cyrillic/Latin letters) verbatim."""
    s = _STEM_CACHE.get(tok)
    if s is None:
        if _RU_WORD_RE.fullmatch(tok):
            s = stem_ru(tok)
        elif tok.isascii() and tok.isalpha():
            s = stem_en(tok)
        else:
            s = tok
        _STEM_CACHE[tok] = s
    return s


def analyze(text: str) -> list[str]:
    """fold → tokenize → stem. index_kb.py feeds " ".join(analyze(x)) of every
    chunk to FTS5; queries and the fallback go through the same function."""
    return [stem_token(t) for t in _TOKEN_RE.findall(_fold(text))]


def _dedupe(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


# ---------- query parsing ----------

# A search unit: (stems, is_prefix, label). One stem = a term; several = a phrase
# (the tokens must stand next to each other, in order). `label` is what the user
# typed, for messages.
Unit = tuple  # tuple[tuple[str, ...], bool, str]

_FAMILY_MIN = 7        # shortest Russian stem that also searches as its word family
_QUOTED_RE = re.compile(r'"([^"]+)"')
_WORD_SEP_RE = re.compile(r"[\s/,;|()\[\]{}<>«»]+")


def parse_query(query: str, stem: bool = True) -> list[Unit]:
    """A natural-language query → the units an OR-ranked search runs on.

    * A plain word is one term: stopwords and single characters dropped, then
      stemmed and deduplicated (two forms of one word are one term).
    * A token glued together by punctuation — `10.0.2.15`, `--mac-address`,
      `vbox_sf`, `hold.setBlocked` — is a PHRASE of its parts: the tokenizer
      splits on that punctuation on both sides, so only adjacency tells
      `10.0.2.15` from any passage that has a 10 and a 15 somewhere. Its
      alphabetic parts also count as terms on their own.
    * `"double quotes"` inside the query make a phrase explicitly.
    Falls back to every token when all of them were stopwords."""
    st = stem_token if stem else (lambda t: t)
    units: list[Unit] = []
    seen: set[tuple[str, ...]] = set()

    def add(stems: list[str], label: str) -> None:
        key = tuple(stems)
        if key and key not in seen:
            seen.add(key)
            units.append((key, False, label))

    for m in _QUOTED_RE.finditer(query):
        toks = _tokenize(m.group(1))
        if toks:
            add([st(t) for t in toks], m.group(1).strip())
    singles: list[str] = []
    for word in _WORD_SEP_RE.split(_QUOTED_RE.sub(" ", query)):
        toks = _tokenize(word)
        if len(toks) == 1:
            singles.append(toks[0])
        elif toks:
            add([st(t) for t in toks], word.strip(".:!?'\"-"))
            singles.extend(t for t in toks if len(t) >= 3 and not t.isdigit())
    content = [t for t in singles if t not in _STOPWORDS and len(t) > 1]
    if not content and not units:
        content = singles
    for t in content:
        add([st(t)], t)
    if stem:
        for stems, _, label in list(units):
            fam = _family(stems[0]) if len(stems) == 1 else None
            if fam and (fam,) not in seen:
                seen.add((fam,))
                units.append(((fam,), True, label))
    return units


def _family(stem: str) -> Optional[str]:
    """Prefix that stands for the word family of a long Russian stem.

    Snowball strips inflection, not derivation: «блокировка» / «блокирует»,
    «эпидемический» / «эпидемия», «виртуализация» / «виртуальный» keep
    different stems, and a query in one form misses the passage written in
    another. The first ~60 % of the stem (at least 6 letters) is their common
    part. Searched as one more OR unit it costs little precision: BM25 gives a
    broad prefix a low weight by itself, and a passage with the exact word
    still collects both units."""
    n = len(stem)
    if n < _FAMILY_MIN or not _RU_WORD_RE.fullmatch(stem):
        return None
    return stem[:6 if n <= 9 else round(n * 0.62)]


def _unit_expr(unit: Unit) -> str:
    stems, prefix, _ = unit
    return '"' + " ".join(stems) + '"' + (" *" if prefix else "")


def _match_expr(units: list[Unit], require_all: bool) -> str:
    if not units:
        return '""'
    return (" AND " if require_all else " OR ").join(_unit_expr(u) for u in units)


# ---------- knowledge-base handle ----------

class KB:
    """An opened `_index.db` plus what the searcher needs to know about it."""

    def __init__(self, kb_dir: Path):
        self.dir = Path(kb_dir)
        db_path = self.dir / INDEX_DB_NAME
        if not db_path.is_file():
            raise FileNotFoundError(
                f"no {INDEX_DB_NAME} in {self.dir} — build it: dkb index {self.dir} "
                f"(or `ensure_env.py index_kb.py {self.dir}`)")
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        try:
            self.meta = {r["key"]: r["value"] for r in
                         self.conn.execute("SELECT key, value FROM meta")}
        except sqlite3.OperationalError:
            self.meta = {}
        analyzer = self.meta.get("analyzer")
        if analyzer is not None and analyzer != ANALYZER:
            raise sqlite3.OperationalError(
                f"index analyzer {analyzer!r} differs from this searcher's {ANALYZER!r} — "
                f"rebuild the index: dkb index {self.dir}")
        self.stemmed = analyzer is not None      # False: legacy pre-stemming index
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(chunks)")}
        self.has_lines = "line_start" in cols
        dcols = {r[1] for r in self.conn.execute("PRAGMA table_info(docs)")}
        self.has_lang = "lang" in dcols
        self.fts = self._probe_fts()
        self.n_chunks = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        self.analyzer: Callable[[str], list[str]] = analyze if self.stemmed else _tokenize

    def _probe_fts(self) -> bool:
        if os.environ.get("DOC2KB_NO_FTS"):          # diagnostics: force the fallback
            return False
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
        ).fetchone()
        if not row:
            return False
        # The table can exist in the schema yet fail to load if this interpreter's
        # sqlite lacks the FTS5 module — probe a no-op MATCH to be certain.
        try:
            self.conn.execute("SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH 'a' LIMIT 0")
            return True
        except sqlite3.OperationalError:
            return False

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def lang_shares(self) -> dict[str, float]:
        """Share of the corpus (by tokens) per document language tag."""
        if not self.has_lang:
            return {}
        rows = self.conn.execute(
            "SELECT lang, SUM(MAX(tokens_estimated, 1)) FROM docs GROUP BY lang").fetchall()
        total = sum(r[1] or 0 for r in rows) or 1
        return {r[0]: (r[1] or 0) / total for r in rows if r[0]}


def _filter_sql(doc: Optional[str], stype: Optional[str], page: Optional[int],
                alias: str = "c") -> tuple[str, list[Any]]:
    clauses, params = [], []
    if doc:
        # A doc id also matches its embedded documents (`doc-001_e01`, …).
        clauses.append(f"({alias}.doc_id = ? OR {alias}.doc_id LIKE ? ESCAPE '\\')")
        params += [doc, doc.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "\\_e%"]
    if stype:
        clauses.append(f"{alias}.source_type = ?")
        params.append(stype)
    if page is not None:
        clauses.append(f"{alias}.page = ?")
        params.append(page)
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


# ---------- FTS5 query path ----------

# FTS5 query-syntax lexer: "strings" ("" escapes a quote), barewords (FTS5's
# bareword alphabet), whitespace, any other single character.
_RAW_LEX_RE = re.compile(r'"(?:[^"]|"")*"|[A-Za-z0-9_\x1a\u0080-\U0010ffff]+|\s+|.', re.S)


def _rewrite_raw(expr: str) -> tuple[str, list[str], list[str]]:
    """Run every search term of an FTS5 MATCH expression through the analyzer
    while leaving its syntax intact: operators (AND OR NOT), NEAR(... , N),
    column filters (`body:` / `{context body}:`), `^`, `+`, `*`, parentheses.
    A prefix term (`управлени*`) is stemmed too, so it is a prefix of the
    stems it should match. Returns (expression, terms, prefixes) — the last
    two drive snippet highlighting."""
    toks = _RAW_LEX_RE.findall(expr)
    sig = [i for i, t in enumerate(toks) if not t.isspace()]
    nxt = {a: toks[b] for a, b in zip(sig, sig[1:])}
    prv = {b: toks[a] for a, b in zip(sig, sig[1:])}
    out: list[str] = []
    terms: list[str] = []
    prefixes: list[str] = []
    in_braces = False
    for i, t in enumerate(toks):
        if t == "{":
            in_braces = True
        elif t == "}":
            in_braces = False
        quoted = t.startswith('"') and len(t) >= 2
        bare = not quoted and _RAW_LEX_RE.fullmatch(t) is not None and (
            t[0].isalnum() or t[0] in "_\x1a" or ord(t[0]) >= 0x80)
        if not (quoted or bare):
            out.append(t)
            continue
        if bare and (in_braces or t in ("AND", "OR", "NOT")
                     or (t == "NEAR" and nxt.get(i) == "(")
                     or nxt.get(i) == ":"
                     or (prv.get(i) == "," and t.isdigit())):
            out.append(t)
            continue
        text = t[1:-1].replace('""', '"') if quoted else t
        ts = analyze(text)
        if ts and nxt.get(i) == "*":
            prefixes.append(ts[-1])
            ts_plain = ts[:-1]
        else:
            ts_plain = ts
        terms.extend(ts_plain)
        if out and out[-1].endswith('"'):
            out.append(" ")  # `"a""b"` would read as ONE string with an escaped quote
        out.append('"' + " ".join(ts) + '"')
    return "".join(out), _dedupe(terms), _dedupe(prefixes)


_K1, _B = 1.2, 0.75          # FTS5 bm25() constants
_COL_WEIGHTS = (2.0, 1.0)    # context, body
_HIT_COLS = ("c.chunk_id, c.doc_id, c.source_path, c.kb_path, c.source_type, c.page, "
             "c.heading, c.body, c.ord, c.tokens, c.context")


def _hit_cols(kb: KB) -> str:
    return _HIT_COLS + (", c.line_start, c.line_end" if kb.has_lines
                        else ", NULL AS line_start, NULL AS line_end")


def _idf(n_rows: int, n_hit: int) -> float:
    v = math.log((n_rows - n_hit + 0.5) / (n_hit + 0.5))
    return v if v > 0.0 else 1e-6


def _fts_rows(kb: KB, match: str, limit: int, where_extra: str,
              params: list[Any]) -> list[dict[str, Any]]:
    # bm25(): context column weighted 2x over body so a hit in the heading path /
    # title (the contextual header) outranks an incidental body match. More
    # negative = more relevant, hence ASC; chunk_id breaks ties deterministically
    # (the python fallback sorts the same way).
    sql = f"""
        SELECT {_hit_cols(kb)}, bm25(chunks_fts, 2.0, 1.0) AS score
        FROM chunks_fts
        JOIN chunks c ON c.chunk_id = chunks_fts.rowid
        WHERE chunks_fts MATCH ?{where_extra}
        ORDER BY score ASC, c.chunk_id ASC
        LIMIT ?
    """
    out = []
    for r in kb.conn.execute(sql, [match, *params, limit]):
        d = dict(r)
        d["score"] = -float(d["score"])      # flip to higher-is-better
        out.append(d)
    return out


def _fts_df(kb: KB, unit: Unit) -> int:
    try:
        return kb.conn.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
            [_unit_expr(unit)]).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


# ---------- pure-Python BM25 fallback ----------

def _count_unit(tokens: list[str], stems: tuple, prefix: bool) -> int:
    if prefix:
        p = stems[0]
        return sum(1 for t in tokens if t.startswith(p))
    if len(stems) == 1:
        return tokens.count(stems[0])
    n, first, c, i = len(stems), stems[0], 0, 0
    try:
        while True:
            i = tokens.index(first, i)
            if tuple(tokens[i:i + n]) == stems:
                c += 1
            i += 1
    except ValueError:
        return c


def _py_search(kb: KB, units: list[Unit], limit: int, where_extra: str, params: list[Any],
               require_all: bool) -> tuple[list[dict[str, Any]], list[int]]:
    """FTS5's bm25() re-implemented over chunks analyzed on the fly: per-unit
    f = Σ column_weight × occurrences, D = row length in tokens (unweighted),
    avgdl and IDF = ln((N − n + 0.5)/(n + 0.5)) (≤ 0 → 1e-6) over the WHOLE
    table, ignoring filters — exactly what FTS5 does, so the ranking matches
    the FTS5 path hit for hit. Returns (hits, document frequency per unit)."""
    df = [0] * len(units)
    if not units:
        return [], df
    keep = {r[0] for r in kb.conn.execute(
        f"SELECT chunk_id FROM chunks c WHERE 1=1{where_extra}", params)}
    an = kb.analyzer
    n_rows = n_tokens = 0
    cand: list[tuple[int, int, list[float]]] = []   # (chunk_id, D, f per unit)
    wc, wb = _COL_WEIGHTS
    for cid, ctx_text, body_text in kb.conn.execute(
            "SELECT chunk_id, context, body FROM chunks ORDER BY chunk_id"):
        ctx, body = an(ctx_text), an(body_text)
        n_rows += 1
        dl = len(ctx) + len(body)
        n_tokens += dl
        freqs = []
        present = 0
        for j, (stems, prefix, _) in enumerate(units):
            f = wc * _count_unit(ctx, stems, prefix) + wb * _count_unit(body, stems, prefix)
            if f:
                df[j] += 1
                present += 1
            freqs.append(f)
        if present and cid in keep and (present == len(units) or not require_all):
            cand.append((cid, dl, freqs))
    if not cand:
        return [], df
    avgdl = n_tokens / n_rows
    idf = [_idf(n_rows, n) for n in df]
    scored = []
    for cid, dl, freqs in cand:
        score = 0.0
        for j, f in enumerate(freqs):
            score += idf[j] * ((f * (_K1 + 1.0)) / (f + _K1 * (1 - _B + _B * dl / avgdl)))
        scored.append((-score, cid))
    scored.sort()
    top = scored[:limit]
    by_id = {r["chunk_id"]: dict(r) for r in kb.conn.execute(
        f"SELECT {_hit_cols(kb)} FROM chunks c "
        f"WHERE c.chunk_id IN ({','.join('?' * len(top))})", [cid for _, cid in top])}
    out = []
    for neg, cid in top:
        d = by_id[cid]
        d["score"] = -neg
        out.append(d)
    return out, df


# ---------- search ----------

def search_kb(kb: KB, query: str, *, top_k: int = 6, doc: Optional[str] = None,
              stype: Optional[str] = None, page: Optional[int] = None,
              raw: bool = False, require_all: bool = False,
              fallback: bool = True) -> dict[str, Any]:
    """Rank the passages of one KB for one query.

    Returns {hits, notes, terms, prefixes, absent}. Each hit carries `score`
    (BM25, higher is better) and `fit` — the score as a share of the best a
    passage could get for this query in this KB (0..1). `fit` is comparable
    between KBs, which raw BM25 is not (its IDF depends on the corpus).

    Nothing found is retried before it is reported: all-terms → any-term, then
    a term the index has never seen → the same term as a prefix."""
    where_extra, params = _filter_sql(doc, stype, page)
    notes: list[str] = []
    fetch = top_k + 6                      # headroom: adjacent chunks merge into one passage
    out: dict[str, Any] = {"hits": [], "notes": notes, "terms": set(), "prefixes": [],
                           "absent": []}

    if raw and kb.fts:
        if kb.stemmed:
            match, terms, prefixes = _rewrite_raw(query)
        else:
            match, terms, prefixes = query, [t for t in _tokenize(query)], []
        hits = _fts_rows(kb, match, fetch, where_extra, params)
        for h in hits:
            h["fit"] = None
        out.update(hits=hits, terms=set(terms), prefixes=prefixes)
        return out

    units = parse_query(query, stem=kb.stemmed)
    if not units:
        return out

    pool = max(fetch, _RERANK_POOL)

    def run(us: list[Unit], all_terms: bool) -> tuple[list[dict[str, Any]], list[int]]:
        if kb.fts:
            dfs = [_fts_df(kb, u) for u in us]
            rows = _fts_rows(kb, _match_expr(us, all_terms), pool, where_extra, params)
            return rows, dfs
        return _py_search(kb, us, pool, where_extra, params, all_terms)

    hits, dfs = run(units, require_all)

    # A term the index has never seen may be a clipped form or another member of
    # its word family — retry it as a prefix, shortening until something matches.
    swapped: list[tuple[int, str]] = []
    probe = list(units)
    for k, (u, n) in enumerate(zip(units, dfs)):
        stems, is_prefix, label = u
        if not fallback or n or is_prefix or len(stems) != 1 or len(stems[0]) < 4:
            continue
        stem = stems[0]
        for cut in range(len(stem), max(3, len(stem) - 6), -1):
            pu = ((stem[:cut],), True, label)
            j = next((i for i, x in enumerate(probe) if x[0] == pu[0] and x[1]), None)
            if j is not None:
                if dfs[j]:                         # its family prefix already matches
                    break
                continue
            # FTS5 can count a prefix cheaply; the fallback tries the whole stem only
            if (_fts_df(kb, pu) if kb.fts else cut == len(stem)):
                probe[k] = pu
                swapped.append((k, f"{label} → {stem[:cut]}*"))
                break
    if swapped:
        hits2, dfs2 = run(probe, require_all)
        took = [text for k, text in swapped if dfs2[k]]
        if took:
            units, hits, dfs = probe, hits2, dfs2
            notes.append("no exact match, searched as a prefix: " + ", ".join(took))
    if not hits and require_all and len(units) > 1 and fallback:
        hits, dfs = run(units, False)
        if hits:
            notes.append("no passage has ALL the terms — showing any-term matches")

    hits = _rerank(kb, units, dfs, hits)[:fetch]
    found = {u[2] for u, n in zip(units, dfs) if n}
    absent = _dedupe(u[2] for u, n in zip(units, dfs) if n == 0 and u[2] not in found)
    # `fit` is measured against the units that exist in this KB at all — a family
    # prefix nobody matches must not make every passage look like a poor fit.
    best = sum(_idf(kb.n_chunks, n) for u, n in zip(units, dfs) if n or not u[1]) * (_K1 + 1.0)
    for h in hits:
        h["fit"] = (h["score"] / best) if best > 0 else 0.0
    terms = {s for u in units if not u[1] for s in u[0]}
    out.update(hits=hits, terms=terms, prefixes=[u[0][0] for u in units if u[1]],
               absent=absent)
    return out


_RERANK_POOL = 40
_COVERAGE_FLOOR = 0.5
_NAV_PENALTY = 0.6
_NAV_HEADING_RE = re.compile(
    r"^(?:table of contents|contents|references|bibliography|index|works cited|"
    r"содержание|оглавление|литература|список литературы|библиография|"
    r"библиографический список|список (?:использованных )?источников|"
    r"предметный указатель|алфавитный указатель)\W*$", re.I)
_NAV_PART_RE = re.compile(r"/\d+-(?:references|bibliography|contents)\.md$")
_TOC_LINE_RE = re.compile(r"(?:\.{4,}|…{2,}|(?:\. ){4,})\s*\d+\s*\|?\s*$", re.M)
_REF_LINE_RE = re.compile(r"^\s*(?:[-*>]\s*)?\[?\d{1,3}[\].)]\s+\S", re.M)


def _is_navigation(h: dict[str, Any]) -> bool:
    """A table of contents, a reference list or an index: it repeats the words
    of every section title / every cited paper, so it matches almost any query
    — and answers none."""
    last = (h.get("heading") or "").split(" › ")[-1].strip()
    if last and _NAV_HEADING_RE.match(last):
        return True
    if _NAV_PART_RE.search(h.get("kb_path") or ""):      # a shard named after its chapter
        return True
    body = h["body"]
    if len(_TOC_LINE_RE.findall(body)) >= 4:
        return True
    lines = body.count("\n") + 1
    return lines >= 6 and len(_REF_LINE_RE.findall(body)) >= 0.6 * lines and \
        len(re.findall(r"\b(?:19|20)\d\d\b", body)) >= 0.5 * lines


def _rerank(kb: KB, units: list[Unit], dfs: list[int],
            hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """BM25 × how much of the query the passage covers, navigation pages down.

    BM25 alone lets a passage that repeats one rare word outrank a passage that
    has four of the five things asked about. Coverage is the IDF-weighted share
    of the query's concepts present in the passage (a word and its family
    prefix are one concept); the score is scaled between _COVERAGE_FLOOR (one
    stray term) and 1 (everything). Applied to the BM25 top _RERANK_POOL, the
    same way for the FTS5 and the pure-Python backend."""
    if len(hits) < 2:
        return hits
    for h in hits:
        h["bm25"] = h["score"]
        if _is_navigation(h):
            h["score"] *= _NAV_PENALTY
    hits.sort(key=lambda h: (-h["score"], h["chunk_id"]))
    concepts: dict[str, float] = {}
    for (stems, prefix, label), n in zip(units, dfs):
        if n:
            concepts[label] = max(concepts.get(label, 0.0), _idf(kb.n_chunks, n))
    total = sum(concepts.values())
    if len(concepts) < 2 or total <= 0:
        return hits
    an = kb.analyzer
    for h in hits:
        toks = an(h.get("context") or "") + ["\x00"] + an(h["body"])
        got = {label for (stems, prefix, label), n in zip(units, dfs)
               if n and _count_unit(toks, stems, prefix)}
        cov = sum(concepts[c] for c in got) / total
        h["score"] = h["score"] * (_COVERAGE_FLOOR + (1.0 - _COVERAGE_FLOOR) * cov)
    hits.sort(key=lambda h: (-h["score"], h["chunk_id"]))
    return hits


# ---------- passages ----------

_MERGE_MAX_TOKENS = 1100
_STRONG_SHARE = 0.75    # of the best hit's score: full text / merging only above it
_WEAK_SHARE = 0.4       # … and below this a hit is not worth even a pointer


def _rank_key(h: dict[str, Any]) -> float:
    return h["fit"] if h.get("fit") is not None else h["score"]


def _to_passages(hits: list[dict[str, Any]], top_k: int,
                 drop_weak: bool = False) -> list[dict[str, Any]]:
    """Ranked chunks → ranked passages. Chunks that sit next to each other in
    one file become one passage (one header, one line range) when both are
    strong matches — a weak neighbour would only pad the answer. Order: best
    chunk first."""
    if not hits:
        return []
    floor = max(_rank_key(h) for h in hits) * _STRONG_SHARE
    by_file: dict[tuple[Any, str], list[dict[str, Any]]] = {}
    for h in hits:
        h["strong"] = _rank_key(h) >= floor
        by_file.setdefault((h.get("kb"), h["kb_path"]), []).append(h)
    passages: list[dict[str, Any]] = []
    for group in by_file.values():
        group.sort(key=lambda h: h["ord"] if h.get("ord") is not None else 0)
        run: list[dict[str, Any]] = []
        for h in group:
            if (run and h["strong"] and run[-1]["strong"]
                    and h.get("ord") is not None and run[-1].get("ord") is not None
                    and h["ord"] == run[-1]["ord"] + 1
                    and sum(x.get("tokens") or 0 for x in run) + (h.get("tokens") or 0)
                    <= _MERGE_MAX_TOKENS):
                run.append(h)
            else:
                if run:
                    passages.append(_merge(run))
                run = [h]
        if run:
            passages.append(_merge(run))
    passages.sort(key=lambda p: (-(p["rank_key"]), p["kb_path"], p["chunk_ids"][0]))
    if drop_weak:
        weak = floor / _STRONG_SHARE * _WEAK_SHARE
        passages = [p for i, p in enumerate(passages) if i == 0 or p["rank_key"] >= weak]
    return passages[:top_k]


def _merge(run: list[dict[str, Any]]) -> dict[str, Any]:
    best = max(run, key=lambda h: h["score"])
    first, last = run[0], run[-1]
    pages = [h["page"] for h in run if h.get("page") is not None]
    return {
        "kb": first.get("kb"), "kb_path": first["kb_path"], "doc_id": first["doc_id"],
        "source_path": first["source_path"], "source_type": first["source_type"],
        "heading": best.get("heading") or first.get("heading"),
        "page": pages[0] if pages else None,
        "page_end": pages[-1] if pages and pages[-1] != pages[0] else None,
        "line_start": first.get("line_start"), "line_end": last.get("line_end"),
        "body": "\n\n".join(h["body"] for h in run),
        "tokens": sum(h.get("tokens") or max(1, len(h["body"]) // 3) for h in run),
        "score": best["score"], "fit": best.get("fit"),
        "rank_key": _rank_key(best), "strong": best["strong"],
        "chunk_ids": [h["chunk_id"] for h in run],
    }


# ---------- snippets ----------

_SNIP_WORD_RE = re.compile(r"[\w\u0300-\u036f]+")


def _make_snippet(body: str, qset: set[str], prefixes: Iterable[str] = (),
                  analyzer: Callable[[str], list[str]] = analyze,
                  width: int = 16) -> str:
    """Original-text window of `width` words densest in query terms, matching
    words wrapped in «». Words are matched through the analyzer, so
    «затратами» highlights for "затраты"."""
    words = list(_SNIP_WORD_RE.finditer(body))
    if not words:
        return re.sub(r"\s+", " ", body).strip()[:200]
    prefixes = tuple(prefixes)
    hits: list[frozenset[str]] = []
    for m in words:
        hs = frozenset(t for t in analyzer(m.group())
                       if t in qset or (prefixes and t.startswith(prefixes)))
        hits.append(hs)
    n = len(words)
    w = min(width, n)
    # Sliding window scored by (distinct query terms, matching words).
    cnt: dict[str, int] = {}
    n_hit = 0

    def _add(hs: frozenset, d: int) -> None:
        for t in hs:
            c = cnt.get(t, 0) + d
            if c:
                cnt[t] = c
            else:
                del cnt[t]

    for j in range(w):
        _add(hits[j], 1)
        n_hit += bool(hits[j])
    best, best_key = 0, (len(cnt), n_hit)
    for i in range(1, n - w + 1):
        _add(hits[i - 1], -1)
        _add(hits[i + w - 1], 1)
        n_hit += bool(hits[i + w - 1]) - bool(hits[i - 1])
        key = (len(cnt), n_hit)
        if key > best_key:
            best, best_key = i, key
    hit_idx = [j for j in range(best, best + w) if hits[j]]
    if hit_idx:  # centre the hits in the window, never dropping one
        centre = (hit_idx[0] + hit_idx[-1]) // 2 - w // 2
        best = max(hit_idx[-1] - w + 1, min(hit_idx[0], centre), 0)
        best = min(best, n - w)
    parts = []
    for j in range(best, best + w):
        m = words[j]
        if j > best:
            parts.append(body[words[j - 1].end():m.start()])
        parts.append(f"«{m.group()}»" if hits[j] else m.group())
    snip = re.sub(r"\s+", " ", "".join(parts)).strip()
    if best > 0:
        snip = "… " + snip
    if best + w < n:
        snip += " …"
    return snip


# ---------- discovery ----------

_SKIP_DIRS = frozenset(("node_modules", "__pycache__", "venv", "site-packages", "Library",
                        "assets", "raw", "docs", "dist", "build", "target"))


def is_kb(d: Path) -> bool:
    try:
        return (d / "docs").is_dir() and ((d / INDEX_DB_NAME).is_file()
                                          or (d / "manifest.json").is_file())
    except OSError:
        return False


def discover_kbs(root: Path, max_depth: int = 5, limit: int = 64,
                 budget_s: float = 2.0) -> list[Path]:
    """Every doc2kb KB at or under `root` (a KB is a folder with `docs/` and an
    `_index.db` or `manifest.json`), a KB kept inside another KB's folder
    included. Does not look into `docs/` / `assets/` / `raw/`, a hidden folder,
    a KB's own `_`-folders or a dependency tree; stops at `max_depth`, `limit`
    KBs or `budget_s`."""
    root = Path(root)
    found: list[Path] = [root] if is_kb(root) else []
    deadline = time.monotonic() + budget_s
    level = [root]
    for _ in range(max_depth):
        nxt: list[Path] = []
        for d in level:
            # inside a KB the `_`-folders are its own (_logs, _media, _mineru …)
            hidden = (".", "_") if d in found else (".",)
            try:
                with os.scandir(d) as it:
                    subs = sorted(e.path for e in it
                                  if e.is_dir(follow_symlinks=False)
                                  and not e.name.startswith(hidden)
                                  and e.name not in _SKIP_DIRS)
            except OSError:
                continue
            for s in subs:
                p = Path(s)
                if is_kb(p):
                    found.append(p)
                    if len(found) >= limit:
                        return found
                nxt.append(p)
            if time.monotonic() > deadline:
                return found
        level = nxt
        if not level:
            break
    return found


def resolve_kbs(root: Path, select: Optional[list[str]] = None) -> list[Path]:
    """KBs to search: `root` itself or everything under it; `select` narrows the
    set — each entry is a path to a KB, or a piece of a KB's path."""
    root = Path(root).expanduser().resolve()
    kbs = discover_kbs(root)
    if not select:
        return kbs
    out: list[Path] = []
    for s in select:
        p = Path(s).expanduser()
        cand = p if p.is_absolute() else root / p
        if is_kb(cand):
            got = [cand.resolve()]
        elif cand.is_dir():
            got = discover_kbs(cand.resolve())
        else:
            needle = s.strip("/").casefold()
            got = [k for k in kbs if needle in str(k).casefold()]
        for k in got:
            if k not in out:
                out.append(k)
    return out


# ---------- keeping the index current ----------

def docs_listing(kb_dir: Path) -> dict[str, tuple[int, int]]:
    """{`docs/…md`: (size, mtime_ns)} for every Markdown file of a KB — flat
    docs and the parts of sharded ones. index_kb.py stores it in the `files`
    table; a different listing later means the index is behind the docs."""
    out: dict[str, tuple[int, int]] = {}
    try:
        with os.scandir(Path(kb_dir) / "docs") as it:
            for e in it:
                if e.name.endswith(".md") and e.is_file():
                    st = e.stat()
                    out["docs/" + e.name] = (st.st_size, st.st_mtime_ns)
                elif e.is_dir() and not e.name.startswith("."):
                    try:
                        with os.scandir(e.path) as it2:
                            for f in it2:
                                if f.name.endswith(".md") and f.is_file():
                                    st = f.stat()
                                    out[f"docs/{e.name}/{f.name}"] = (st.st_size, st.st_mtime_ns)
                    except OSError:
                        pass
    except OSError:
        pass
    return out


def _engine_rev_of(path: Path) -> int:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            m = re.search(r"^ENGINE_REV\s*=\s*(\d+)", f.read(24000), re.M)
        return int(m.group(1)) if m else 0
    except OSError:
        return 0


def index_state(kb_dir: Path) -> Optional[str]:
    """Why this KB's index needs a refresh, or None when it is current."""
    kb_dir = Path(kb_dir)
    db_path = kb_dir / INDEX_DB_NAME
    if not db_path.is_file():
        return "no index yet"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            if meta.get("schema_version") != INDEX_SCHEMA or meta.get("analyzer") != ANALYZER:
                return f"index format {meta.get('schema_version', '?')} → {INDEX_SCHEMA}"
            stored = {r[0]: (r[1], r[2]) for r in
                      conn.execute("SELECT path, size, mtime_ns FROM files")}
        finally:
            conn.close()
    except sqlite3.Error:
        return "index unreadable"
    now = docs_listing(kb_dir)
    if now != stored:
        n = len(set(now.items()) ^ set(stored.items()))
        return f"docs changed: {max(1, n // 2 if len(now) == len(stored) else n)} file(s)"
    return None


def _indexer() -> Any:
    """index_kb, when this file runs from the skill (not as a KB's `_query.py`,
    which travels alone)."""
    here = Path(__file__).resolve().parent
    if not (here / "index_kb.py").is_file() or not (here / "_common.py").is_file():
        return None
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    try:
        import index_kb  # type: ignore
        return index_kb
    except Exception:
        return None


def ensure_fresh(kb_dir: Path, quiet: bool = False) -> Optional[str]:
    """Bring a KB's index (and its bundled searcher) up to date before a query,
    so nobody has to remember to re-index after editing docs or upgrading
    doc2kb. Returns a note for the reader when the index is known to be stale
    and could NOT be refreshed; None otherwise. Never raises."""
    kb_dir = Path(kb_dir)
    if os.environ.get("DOC2KB_NO_AUTOINDEX") or not (kb_dir / "docs").is_dir():
        return None
    try:
        why = index_state(kb_dir)
    except Exception:
        return None
    ix = _indexer()
    if why is None:
        if ix is not None and _engine_rev_of(kb_dir / "_query.py") < ENGINE_REV:
            try:                                  # index is fine, bundled searcher is old
                ix._write_launchers(kb_dir)
            except Exception:
                pass
        return None
    usable = (kb_dir / INDEX_DB_NAME).is_file()
    if ix is None:
        return (f"index is stale ({why}) and this copy of the searcher cannot rebuild it — "
                f"run: dkb index {kb_dir}") if usable else None
    t0 = time.monotonic()
    kwargs: dict[str, Any] = {}
    try:
        if usable:                                # keep the KB's own chunk parameters
            c = sqlite3.connect(f"file:{kb_dir / INDEX_DB_NAME}?mode=ro", uri=True)
            try:
                m = dict(c.execute("SELECT key, value FROM meta").fetchall())
            finally:
                c.close()
            if m.get("chunk_target_tokens") and m.get("chunk_max_tokens"):
                kwargs = {"target": int(m["chunk_target_tokens"]),
                          "cap": int(m["chunk_max_tokens"])}
    except (sqlite3.Error, ValueError):
        kwargs = {}
    try:
        if not quiet and not usable:
            print(f"doc2kb: building the search index of {kb_dir} …", file=sys.stderr)
        res = ix.build_index(kb_dir, wait=0.0 if usable else 120.0, **kwargs)
        if not quiet and res.get("status") == "built":
            print(f"doc2kb: search index refreshed ({why}) — {res.get('chunks')} passages, "
                  f"{time.monotonic() - t0:.1f}s", file=sys.stderr)
        return None
    except Exception as e:                        # busy, read-only folder, broken doc …
        if type(e).__name__ == "IndexBusy":
            return None                           # someone else is refreshing it right now
        return f"index is stale ({why}); refresh failed: {e}" if usable else None


# ---------- rendering ----------

def _loc(p: dict[str, Any]) -> str:
    if p.get("line_start") is not None:
        return f"{p['kb_path']}:{p['line_start']}-{p['line_end']}"
    return str(p["kb_path"])


def _short_heading(heading: Optional[str], keep: int = 2, width: int = 110) -> str:
    """The last `keep` levels of a heading path — the section the passage is
    in. The levels above it (book title, part) repeat on every hit."""
    if not heading:
        return ""
    parts = [x for x in str(heading).split(" › ") if x]
    s = " › ".join(parts[-keep:])
    return s if len(s) <= width else s[:width - 1].rstrip() + "…"


def _head(p: dict[str, Any], scores: bool = False) -> str:
    bits = [_loc(p)]
    h = _short_heading(p.get("heading"))
    if h:
        bits.append(h)
    if p.get("page") is not None:
        bits.append(f"p.{p['page']}" + (f"-{p['page_end']}" if p.get("page_end") else ""))
    if scores:
        fit = f" fit {p['fit']:.2f}" if p.get("fit") is not None else ""
        bits.append(f"score {p['score']:.1f}{fit}")
    return " · ".join(bits)


def _source_name(source_path: str) -> str:
    """File name of a source (an embedded document keeps its `#member` tail)."""
    head, sep, tail = str(source_path).partition("#")
    return head.replace("\\", "/").rsplit("/", 1)[-1] + (sep + tail.rsplit("/", 1)[-1] if sep else "")


def _cite(p: dict[str, Any]) -> str:
    bits = [p.get("source_path") or p.get("doc_id") or "?"]
    if p.get("heading"):
        bits.append(str(p["heading"]))
    cite = " › ".join(bits)
    if p.get("page") is not None:
        cite += f"  (page {p['page']})"
    return cite


def _lang_hint(kbs: list[KB], queries: list[str]) -> Optional[str]:
    """Lexical search does not cross languages: a Russian query cannot reach
    the English half of a corpus. Say so once, when it applies."""
    ru_only = en_only = False
    any_ru = False
    for q in queries:
        words = [t for t in _tokenize(q) if len(t) >= 3 and not t.isdigit()]
        cyr = sum(1 for t in words if _RU_WORD_RE.fullmatch(t))
        lat = sum(1 for t in words if t.isascii() and t.isalpha())
        any_ru = any_ru or cyr >= 2
        # a Russian question may carry a few Latin identifiers (NAT, VBoxManage) …
        ru_only = ru_only or (cyr >= 2 and lat * 3 <= cyr)
        # … and a few identifiers alone are not an English question
        en_only = en_only or (lat >= 3 and cyr == 0)
    if ru_only and not en_only:
        other, name = "en", "English"
    elif en_only and not any_ru:
        other, name = "ru", "Russian"
    else:
        return None
    shares = [kb.lang_shares().get(other, 0.0) for kb in kbs]
    share = max(shares) if shares else 0.0
    if share < 0.2:
        return None
    return (f"note: {round(share * 100)}% of this corpus is {name}-language and matches "
            f"only {name} words — add the question in {name} as another query")


def run_search(kb_dirs: list[Path], queries: list[str], *, top_k: int = 6,
               budget: int = 1500, mode: str = "auto", doc: Optional[str] = None,
               stype: Optional[str] = None, page: Optional[int] = None,
               raw: bool = False, require_all: bool = False, as_json: bool = False,
               cwd: Optional[Path] = None, refresh: bool = True,
               scores: bool = False, keep_weak: bool = False,
               show_kb: bool = False) -> tuple[str, int]:
    """Search one or several KBs for one or several queries; return the rendered
    answer (text for an agent, or JSON) and the number of passages found.

    mode: `auto` — full text of the best passages up to `budget` tokens per
    query, the rest as one-line pointers; `full` — full text of all `top_k`;
    `brief` — pointers only."""
    notes: list[str] = []
    kbs: list[KB] = []
    for d in kb_dirs:
        if refresh:
            stale = ensure_fresh(d)
            if stale:
                notes.append(f"note: {d.name}: {stale}")
        try:
            kbs.append(KB(d))
        except (FileNotFoundError, sqlite3.Error) as e:
            notes.append(f"note: skipped {d}: {e}")
    try:
        if not kbs:
            return "\n".join(notes + ["no searchable knowledge base."]), 0
        multi_kb = len(kbs) > 1
        here = Path(cwd).resolve() if cwd else None
        shown_full: set[tuple[str, int]] = set()
        blocks: list[str] = []
        payload: list[dict[str, Any]] = []
        total = 0
        for q in queries:
            hits: list[dict[str, Any]] = []
            qnotes: list[str] = []
            kb_notes: dict[str, list[str]] = {}
            qterms: dict[str, tuple[set[str], list[str]]] = {}
            absent_all: Optional[set[str]] = None
            # Several KBs: a term one KB lacks is usually in another, so the
            # "search it as a prefix" retry runs only if no KB answered at all.
            for fallback in ((True,) if not multi_kb else (False, True)):
                for kb in kbs:
                    try:
                        res = search_kb(kb, q, top_k=top_k, doc=doc, stype=stype, page=page,
                                        raw=raw, require_all=require_all, fallback=fallback)
                    except sqlite3.OperationalError as e:
                        if fallback or not multi_kb:
                            qnotes.append(f"note: {kb.dir.name}: query error: {e}")
                        continue
                    for h in res["hits"]:
                        h["kb"] = str(kb.dir)
                    hits.extend(res["hits"])
                    qterms[str(kb.dir)] = (res["terms"], res["prefixes"])
                    kb_notes[str(kb.dir)] = res["notes"]
                    absent = set(res["absent"])
                    absent_all = absent if absent_all is None else (absent_all & absent)
                if hits:
                    break
                if multi_kb and not fallback:
                    absent_all = None
            if absent_all:
                qnotes.append("not in " + ("these KBs" if multi_kb else "this KB") + ": "
                              + ", ".join(sorted(absent_all)))
            passages = _to_passages(hits, top_k, drop_weak=not keep_weak)
            total += len(passages)
            by_kb = {str(kb.dir): kb for kb in kbs}
            # which passages get their full text
            used = 0
            for i, p in enumerate(passages):
                p["rank"] = i + 1
                terms, prefixes = qterms.get(p["kb"], (set(), []))
                p["snippet"] = _make_snippet(p["body"], terms, prefixes,
                                             by_kb[p["kb"]].analyzer)
                key_ids = [(p["kb"], c) for c in p["chunk_ids"]]
                p["seen"] = all(k in shown_full for k in key_ids)
                if mode == "full":
                    p["full"] = True
                elif mode == "brief" or p["seen"]:
                    p["full"] = False
                else:
                    # the best passage always; then every strong one that still fits
                    p["full"] = (i == 0) or (p["strong"] and used + p["tokens"] <= budget)
                if p["full"]:
                    used += p["tokens"]
                    shown_full.update(key_ids)
            if as_json:
                payload.append({"query": q, "notes": qnotes,
                                "hits": [_json_hit(p, multi_kb) for p in passages]})
                continue
            lines: list[str] = []
            if len(queries) > 1:
                lines.append(f"### {q}")
            lines.extend(qnotes)
            if not passages:
                lines.append("no matching passages — try other words (synonyms, the other "
                             "language); for an exact string use grep on docs/.")
                blocks.append("\n".join(lines))
                continue
            order: list[str] = []
            for p in passages:
                if p["kb"] not in order:
                    order.append(p["kb"])
            for kbd in order:
                if multi_kb or show_kb or (here is not None and here != Path(kbd)):
                    lines.append(f"## kb: {kbd}")
                lines.extend(kb_notes.get(kbd) or [])
                mine = [p for p in passages if p["kb"] == kbd]
                for p in mine:
                    if p["full"]:
                        lines.append(f"[{p['rank']}] {_head(p, scores)}")
                        lines.append(p["body"])
                        lines.append("")
                briefs = [p for p in mine if not p["full"]]
                for p in briefs:
                    if p["seen"]:
                        lines.append(f"[{p['rank']}] {_loc(p)} · shown above")
                    else:
                        lines.append(f"[{p['rank']}] {_head(p, scores)} — {p['snippet']}")
                srcs = dict.fromkeys((p["doc_id"], _source_name(p["source_path"]))
                                     for p in mine if p.get("source_path"))
                if srcs:
                    lines.append("sources: " + "; ".join(f"{d} = {s}" for d, s in srcs))
            blocks.append("\n".join(lines).rstrip())
        if as_json:
            data: Any = payload if len(queries) > 1 else payload[0]["hits"]
            return json.dumps(data, ensure_ascii=False, indent=2), total
        hint = _lang_hint(kbs, queries) if not raw else None
        tail = [hint] if hint else []
        return "\n\n".join(x for x in ["\n".join(notes)] + blocks + tail if x), total
    finally:
        for kb in kbs:
            kb.close()


def _json_hit(p: dict[str, Any], multi_kb: bool) -> dict[str, Any]:
    rec = {
        "rank": p["rank"],
        "path": p["kb_path"],
        "lines": [p["line_start"], p["line_end"]] if p.get("line_start") is not None else None,
        "doc_id": p["doc_id"],
        "source_path": p["source_path"],
        "source_type": p["source_type"],
        "page": p["page"],
        "heading": p["heading"],
        "citation": _cite(p),
        "score": round(p["score"], 3),
        "fit": round(p["fit"], 3) if p.get("fit") is not None else None,
        "snippet": p["snippet"],
    }
    if multi_kb:
        rec["kb"] = p["kb"]
    if p["full"]:
        rec["text"] = p["body"]
    return rec


def kb_summary(kb_dir: Path) -> dict[str, Any]:
    """What a KB holds, for `--list` / the MCP `kb_list` tool."""
    kb_dir = Path(kb_dir)
    rec: dict[str, Any] = {"path": str(kb_dir), "indexed": False}
    try:
        kb = KB(kb_dir)
    except (FileNotFoundError, sqlite3.Error):
        return rec
    try:
        rows = kb.conn.execute(
            "SELECT doc_id, source_path, source_type, tokens_estimated FROM docs "
            "WHERE doc_id NOT LIKE '%\\_e%' ESCAPE '\\' ORDER BY doc_id").fetchall()
        rec.update(indexed=True, documents=len(rows), passages=kb.n_chunks,
                   tokens=sum(r["tokens_estimated"] or 0 for r in rows),
                   types=sorted({r["source_type"] for r in rows if r["source_type"]}),
                   langs={k: round(v, 2) for k, v in kb.lang_shares().items()},
                   sources=[r["source_path"] for r in rows if r["source_path"]])
    finally:
        kb.close()
    return rec


def render_list(root: Path, kb_dirs: list[Path], max_sources: int = 6) -> str:
    """The KBs at or under `root`: where, how big, which languages, what is in
    them — enough to pick one (or to see there is none)."""
    if not kb_dirs:
        return f"no doc2kb knowledge base at or under {root}."
    lines = [f"{len(kb_dirs)} knowledge base(s) under {root}:"]
    for d in kb_dirs:
        try:
            rel = str(d.relative_to(root)) if d != root else "."
        except ValueError:
            rel = str(d)
        s = kb_summary(d)
        if not s.get("indexed"):
            lines.append(f"- {rel}  (no search index yet — it is built on the first query)")
            continue
        langs = "/".join(k for k, v in sorted(s["langs"].items(), key=lambda kv: -kv[1])
                         if v >= 0.15)
        lines.append(f"- {rel} — {s['documents']} docs, ~{max(1, s['tokens'] // 1000)}k tokens"
                     + (f", {langs}" if langs else ""))
        srcs = s["sources"]
        shown = "; ".join(_source_name(x) for x in srcs[:max_sources])
        if len(srcs) > max_sources:
            shown += f"; … +{len(srcs) - max_sources} more"
        if shown:
            lines.append(f"    {shown}")
    return "\n".join(lines)


def _print_info(kb_dir: Path) -> int:
    kb = KB(kb_dir)
    try:
        meta = kb.meta
        print(json.dumps({
            "kb_dir": str(kb_dir),
            "index": str(kb_dir / INDEX_DB_NAME),
            "backend": "fts5" if kb.fts else "python-bm25 (FTS5 unavailable)",
            "analyzer": meta.get("analyzer") or "none (legacy index: unstemmed; rebuild to stem)",
            "schema_version": meta.get("schema_version"),
            "searcher": {"engine_rev": ENGINE_REV, "file": str(Path(__file__).resolve())},
            "state": index_state(kb_dir) or "current",
            "documents": kb.conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0],
            "chunks": kb.n_chunks,
            "indexer": meta.get("indexer"),
            "created_at": meta.get("created_at"),
            "chunk_target_tokens": meta.get("chunk_target_tokens"),
            "corpus_signature": meta.get("corpus_signature"),
        }, ensure_ascii=False, indent=2))
    finally:
        kb.close()
    return 0


# ---------- command line ----------

USAGE = """\
usage: query.sh "<question>" ["<another question>" ...] [options]
       query_kb.py <kb_dir | folder with KBs> "<question>" ... [options]

Each quoted argument is its own query; several in one call share one output and
never repeat a passage. Output: the best passages in full (up to --budget tokens
per query) with `file:first-last` line refs, then one-line pointers to the rest.

  -k, --top-k N     passages per query (default 6)
  --budget N        tokens of full passage text per query (default 1500)
  --full            full text of every passage (alias: --show)
  --brief           pointers + snippets only, no passage text
  --doc ID          only this document (and documents embedded in it)
  --type T          only this source type (pdf, docx, pptx, note, ...)
  --page N          only this page / slide
  --and             every term must be present (default: any term, ranked)
  --raw             FTS5 MATCH syntax (phrases, NEAR, AND/OR/NOT, prefix*)
  --kb NAME         with a folder of KBs: only the KBs whose path contains NAME
  --list            list the knowledge bases at or under the folder, then exit
  --json            machine-readable output
  --info            index statistics
  --scores          show each hit's BM25 score and fit (share of the best possible)
  --no-refresh      do not re-index when the docs or doc2kb changed
A query that starts with a dash needs no escaping: unknown options are query text.
"""

_VALUE_FLAGS = {"-k": "top_k", "--top-k": "top_k", "--budget": "budget", "--doc": "doc",
                "--type": "stype", "--page": "page", "--kb": "kb"}
_BOOL_FLAGS = {"--and": "require_all", "--raw": "raw", "--show": "full", "--full": "full",
               "--brief": "brief", "--json": "as_json", "--info": "info", "--list": "list",
               "--no-refresh": "no_refresh", "--no-color": "no_color", "--scores": "scores",
               "-h": "help", "--help": "help"}
_INT_FLAGS = {"top_k", "budget", "page"}


def parse_argv(argv: list[str]) -> dict[str, Any]:
    """Options may stand anywhere; every other argument is a query — including
    one that looks like an option this tool does not have (`--mac-address`)."""
    opts: dict[str, Any] = {"queries": [], "kb": []}
    args = list(argv)
    i = 0
    literal = False
    while i < len(args):
        a = args[i]
        i += 1
        if literal or not a.startswith("-") or a == "-":
            opts["queries"].append(a)
            continue
        if a == "--":
            literal = True
            continue
        name, eq, val = a.partition("=")
        if name in _BOOL_FLAGS and not eq:
            opts[_BOOL_FLAGS[name]] = True
            continue
        if re.fullmatch(r"-k\d+", a):
            name, eq, val = "-k", "=", a[2:]
        if name in _VALUE_FLAGS:
            key = _VALUE_FLAGS[name]
            if not eq:
                if i >= len(args):
                    raise ValueError(f"{name} needs a value")
                val = args[i]
                i += 1
            if key in _INT_FLAGS:
                try:
                    val = int(val)
                except ValueError:
                    raise ValueError(f"{name} needs a number, got {val!r}")
            if key == "kb":
                opts["kb"].append(val)
            else:
                opts[key] = val
            continue
        opts["queries"].append(a)
    return opts


def _delegate(argv: list[str]) -> Optional[int]:
    """Hand the call to a newer installed doc2kb, if there is one. A KB carries
    its own copy of this file (`_query.py`) so it can be searched anywhere —
    but that copy is as old as the KB. Where the skill is installed, the
    installed searcher answers instead, so a KB never has to be rebuilt just to
    get a better search."""
    if os.environ.get("DOC2KB_QUERY_DELEGATED") or os.environ.get("DOC2KB_NO_DELEGATE"):
        return None
    me = Path(__file__).resolve()
    roots = [os.environ.get("DOC2KB_SKILL"),
             os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude", "skills", "doc2kb"),
             os.path.join(os.environ.get("CODEX_HOME") or "~/.codex", "skills", "doc2kb")]
    best, best_rev = None, ENGINE_REV
    for r in roots:
        if not r:
            continue
        cand = Path(r).expanduser() / "scripts" / "query_kb.py"
        try:
            if not cand.is_file() or cand.resolve() == me:
                continue
        except OSError:
            continue
        rev = _engine_rev_of(cand)
        if rev > best_rev:
            best, best_rev = cand, rev
    if best is None:
        return None
    import subprocess
    env = dict(os.environ, DOC2KB_QUERY_DELEGATED="1")
    return subprocess.call([sys.executable, str(best), *argv], env=env)


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    rc = _delegate(argv)
    if rc is not None:
        return rc
    try:
        opts = parse_argv(argv)
    except ValueError as e:
        print(f"query error: {e}", file=sys.stderr)
        return 2
    if opts.get("help") or not opts["queries"]:
        print(USAGE, file=sys.stderr if not opts.get("help") else sys.stdout)
        return 0 if opts.get("help") else 2

    root = Path(opts["queries"].pop(0)).expanduser().resolve()
    queries = [q for q in opts["queries"] if q.strip()]
    if not root.is_dir():
        print(f"not a folder: {root}", file=sys.stderr)
        return 2
    kb_dirs = resolve_kbs(root, opts["kb"] or None)

    if opts.get("list"):
        print(render_list(root, kb_dirs))
        return 0
    if not kb_dirs:
        # Never index a folder just because it has a docs/ — most code repos do.
        print(f"no doc2kb knowledge base at or under {root} (a KB is a folder with docs/ "
              f"and manifest.json or {INDEX_DB_NAME})", file=sys.stderr)
        return 2
    refresh = not opts.get("no_refresh")
    if opts.get("info"):
        if refresh:
            ensure_fresh(kb_dirs[0])
        try:
            return _print_info(kb_dirs[0])
        except (FileNotFoundError, sqlite3.Error) as e:
            print(str(e), file=sys.stderr)
            return 2
    if not queries:
        print(USAGE, file=sys.stderr)
        return 2
    mode = "full" if opts.get("full") else "brief" if opts.get("brief") else "auto"
    try:
        text, _ = run_search(
            kb_dirs, queries, top_k=max(1, int(opts.get("top_k", 6))),
            budget=max(0, int(opts.get("budget", 1500))), mode=mode, doc=opts.get("doc"),
            stype=opts.get("stype"), page=opts.get("page"), raw=bool(opts.get("raw")),
            require_all=bool(opts.get("require_all")), as_json=bool(opts.get("as_json")),
            cwd=Path.cwd(), refresh=refresh, scores=bool(opts.get("scores")),
            keep_weak="top_k" in opts or mode != "auto" or bool(opts.get("as_json")))
    except sqlite3.OperationalError as e:
        print(f"query error: {e}", file=sys.stderr)
        return 2
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
