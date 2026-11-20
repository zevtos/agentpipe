#!/usr/bin/env python3
"""
query_kb.py — citation-first search over a doc2kb knowledge base.

This is the retrieval half of the doc2kb "search engine baked into the KB".
`index_kb.py` builds `<kb_dir>/_index.db` (SQLite FTS5 over structure-aware,
heading-path-headed chunks); this script queries it and returns ranked passages
with a citation an agent can drop straight into an answer:

    papers/transformer.pdf › Results › §3.2  (page 7)   [doc-002]

Design constraints (so a SECOND agent / human can run it anywhere):
  * **Pure stdlib.** Only `sqlite3` + the standard library — no venv, no
    third-party deps, no network. `index_kb.py` copies this file verbatim into
    `<kb_dir>/_query.py` so the KB is self-contained and portable: any machine
    with `python3` can search it via `./query.sh "<question>"`.
  * **Morphology in Python.** FTS5's built-in tokenizers stem English only
    (`porter`), and a custom FTS5 tokenizer needs SQLite's C API, which the
    stdlib `sqlite3` module does not expose. So this file carries the analyzer
    — fold → tokenize → Snowball Russian / Snowball English stemmer — and
    `index_kb.py` imports it: index-time and query-time analysis are the same
    function. "затратами на качество" matches "затраты на качество".
  * **FTS5 with an identical fallback.** FTS5 indexes the analyzer's output
    with its `ascii` tokenizer, which reproduces those tokens exactly. If this
    interpreter's SQLite lacks FTS5, the chunks are analyzed on the fly and a
    pure-Python BM25 scores them with FTS5's own `bm25()` formula (k1=1.2,
    b=0.75, same IDF, same column weights), so both backends return the same
    ranking — the fallback is just slower (~0.5 s per 1M tokens).
  * **BM25 sign.** FTS5 `bm25()` returns *more negative = more relevant*; we
    `ORDER BY bm25(...) ASC` and report a flipped (positive, higher-is-better)
    score so the human output reads naturally.
  * **Old indexes still work.** An `_index.db` built before the analyzer
    existed (no `meta.analyzer`) is searched the old way (unstemmed terms);
    a hint to rebuild goes to stderr.

CLI:
    query_kb.py <kb_dir> "<question>" [options]
    query_kb.py <kb_dir> --info

Options:
    -k / --top-k N     number of passages to return (default 8)
    --doc ID           restrict to one doc-id and its embedded docs (e.g. doc-002)
    --type T           restrict to a source_type (pdf, docx, pptx, ipynb, ...)
    --page N           restrict to a single source page number
    --and              require ALL query terms (default: OR + BM25 ranking)
    --raw              FTS5 MATCH syntax (phrases, NEAR, AND/OR/NOT, prefix*);
                       its terms are stemmed like the index
    --show             print the full chunk body, not just a snippet
    --json             machine-readable JSON (one object per hit)
    --no-color         disable ANSI styling in human output

Exit codes: 0 ok (even with zero hits), 2 usage/db error.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

INDEX_DB_NAME = "_index.db"

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


def _content_terms(query: str, stem: bool = True) -> list[str]:
    """Tokenize a natural-language query into ranked-search terms: drop
    stopwords (surface forms), then stem, then dedupe (two forms of one word
    are one term). Falls back to the raw token set if everything was a
    stopword."""
    toks = _tokenize(query)
    terms = [t for t in toks if t not in _STOPWORDS and len(t) > 1] or toks
    if stem:
        terms = [stem_token(t) for t in terms]
    return _dedupe(terms)


# ---------- db helpers ----------

def _open_db(kb_dir: Path) -> sqlite3.Connection:
    db_path = kb_dir / INDEX_DB_NAME
    if not db_path.is_file():
        raise FileNotFoundError(
            f"no {INDEX_DB_NAME} in {kb_dir} — build it with "
            f"`ensure_env.py index_kb.py {kb_dir}` (doc2kb Phase 4.5)"
        )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
    ).fetchone()
    if not row:
        return False
    # The table can exist in the schema yet fail to load if this interpreter's
    # sqlite lacks the FTS5 module — probe a no-op MATCH to be certain.
    try:
        conn.execute("SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH 'a' LIMIT 0")
        return True
    except sqlite3.OperationalError:
        return False


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.OperationalError:
        return {}


def _is_stemmed(conn: sqlite3.Connection) -> bool:
    """True for an index built with this file's analyzer, False for a legacy
    (pre-stemming) index. An index from a NEWER analyzer is refused: its tokens
    can't be reproduced here."""
    analyzer = _meta(conn).get("analyzer")
    if analyzer is None:
        return False
    if analyzer != ANALYZER:
        raise sqlite3.OperationalError(
            f"index analyzer {analyzer!r} differs from this script's {ANALYZER!r} — "
            f"rebuild the index with this doc2kb (index_kb.py <kb_dir>)")
    return True


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


def _build_match(terms: list[str], require_all: bool) -> str:
    if not terms:
        return '""'
    return (" AND " if require_all else " OR ").join(f'"{t}"' for t in terms)


def _search_fts(conn: sqlite3.Connection, query: str, *, top_k: int, doc: Optional[str],
                stype: Optional[str], page: Optional[int], raw: bool,
                require_all: bool) -> list[dict[str, Any]]:
    stemmed = _is_stemmed(conn)
    prefixes: list[str] = []
    if raw and stemmed:
        match, terms, prefixes = _rewrite_raw(query)
    elif raw:
        match, terms = query, _content_terms(query, stem=False)
    else:
        terms = _content_terms(query, stem=stemmed)
        match = _build_match(terms, require_all)
    where_extra, params = _filter_sql(doc, stype, page)
    # bm25(): context column weighted 2x over body so a hit in the heading path /
    # title (the contextual header) outranks an incidental body match. More
    # negative = more relevant, hence ASC; chunk_id breaks ties deterministically
    # (the python fallback sorts the same way). A stemmed index is contentless,
    # so its snippet is built in Python from the original body; a legacy index
    # still uses snippet().
    snip_sql = "NULL" if stemmed else "snippet(chunks_fts, 1, '«', '»', ' … ', 14)"
    sql = f"""
        SELECT c.chunk_id, c.doc_id, c.source_path, c.kb_path, c.source_type,
               c.page, c.heading, c.body,
               bm25(chunks_fts, 2.0, 1.0) AS score, {snip_sql} AS snip
        FROM chunks_fts
        JOIN chunks c ON c.chunk_id = chunks_fts.rowid
        WHERE chunks_fts MATCH ?{where_extra}
        ORDER BY score ASC, c.chunk_id ASC
        LIMIT ?
    """
    rows = conn.execute(sql, [match, *params, top_k]).fetchall()
    out = []
    qset = set(terms)
    for r in rows:
        d = dict(r)
        d["score"] = round(-float(d["score"]), 3)  # flip to higher-is-better
        if d["snip"] is None:
            d["snip"] = _make_snippet(d["body"], qset, prefixes,
                                      analyze if stemmed else _tokenize)
        out.append(d)
    return out


# ---------- pure-Python BM25 fallback ----------

_K1, _B = 1.2, 0.75          # FTS5 bm25() constants
_COL_WEIGHTS = (2.0, 1.0)    # context, body — same as the FTS5 query above


def _search_bm25(conn: sqlite3.Connection, query: str, *, top_k: int, doc: Optional[str],
                 stype: Optional[str], page: Optional[int],
                 require_all: bool) -> list[dict[str, Any]]:
    """FTS5's bm25() re-implemented over chunks analyzed on the fly: per-term
    f = Σ column_weight × occurrences, D = row length in tokens (unweighted),
    avgdl and IDF = ln((N − n + 0.5)/(n + 0.5)) (≤ 0 → 1e-6) over the WHOLE
    table, ignoring filters — exactly what FTS5 does, so the ranking matches
    the FTS5 path hit for hit. A legacy index is tokenized unstemmed, as its
    FTS5 table was."""
    stemmed = _is_stemmed(conn)
    terms = _content_terms(query, stem=stemmed)
    if not terms:
        return []
    where_extra, params = _filter_sql(doc, stype, page)
    keep = {r[0] for r in conn.execute(
        f"SELECT chunk_id FROM chunks c WHERE 1=1{where_extra}", params)}
    if not keep:
        return []

    an = analyze if stemmed else _tokenize
    stream = ((r[0], an(r[1]), an(r[2])) for r in conn.execute(
        "SELECT chunk_id, context, body FROM chunks ORDER BY chunk_id"))
    n_rows = 0
    n_tokens = 0
    df = [0] * len(terms)
    cand: list[tuple[int, int, list[float]]] = []   # (chunk_id, D, f per term)
    wc, wb = _COL_WEIGHTS
    for cid, ctx, body in stream:
        n_rows += 1
        dl = len(ctx) + len(body)
        n_tokens += dl
        freqs = []
        present = 0
        for j, t in enumerate(terms):
            f = wc * ctx.count(t) + wb * body.count(t) if (t in ctx or t in body) else 0.0
            if f:
                df[j] += 1
                present += 1
            freqs.append(f)
        if present and cid in keep and (present == len(terms) or not require_all):
            cand.append((cid, dl, freqs))
    if not cand:
        return []
    avgdl = n_tokens / n_rows
    idf = []
    for n_hit in df:
        v = math.log((n_rows - n_hit + 0.5) / (n_hit + 0.5))
        idf.append(v if v > 0.0 else 1e-6)

    scored = []
    for cid, dl, freqs in cand:
        score = 0.0
        for j, f in enumerate(freqs):
            score += idf[j] * ((f * (_K1 + 1.0)) / (f + _K1 * (1 - _B + _B * dl / avgdl)))
        scored.append((-score, cid))
    scored.sort()
    top = scored[:top_k]
    by_id = {r["chunk_id"]: dict(r) for r in conn.execute(
        "SELECT chunk_id, doc_id, source_path, kb_path, source_type, page, heading, "
        f"body FROM chunks WHERE chunk_id IN ({','.join('?' * len(top))})",
        [cid for _, cid in top])}
    qset = set(terms)
    out = []
    for neg, cid in top:
        d = by_id[cid]
        d["score"] = round(-neg, 3)
        d["snip"] = _make_snippet(d["body"], qset, (), analyze if stemmed else _tokenize)
        out.append(d)
    return out


# ---------- snippets ----------

_SNIP_WORD_RE = re.compile(r"[\ẁ-ͯ]+")


def _make_snippet(body: str, qset: set[str], prefixes: Iterable[str] = (),
                  analyzer: Callable[[str], list[str]] = analyze,
                  width: int = 14) -> str:
    """Original-text window of `width` words densest in query terms, matching
    words wrapped in «» — the same shape FTS5 snippet() produced. Words are
    matched through the analyzer, so «затратами» highlights for "затраты"."""
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


# ---------- rendering ----------

def _cite(hit: dict[str, Any]) -> str:
    bits = [hit.get("source_path") or hit.get("doc_id") or "?"]
    if hit.get("heading"):
        bits.append(str(hit["heading"]))
    cite = " › ".join(bits)
    if hit.get("page") is not None:
        cite += f"  (page {hit['page']})"
    return cite


def _render_human(hits: list[dict[str, Any]], show: bool, color: bool) -> str:
    if not hits:
        return "no matching passages."
    bold = (lambda s: "\033[1m" + s + "\033[0m") if color else (lambda s: s)
    dim = (lambda s: "\033[2m" + s + "\033[0m") if color else (lambda s: s)
    cyan = (lambda s: "\033[36m" + s + "\033[0m") if color else (lambda s: s)
    out: list[str] = []
    for i, h in enumerate(hits, 1):
        tag = "[{0}] score {1}".format(h.get("doc_id"), h.get("score"))
        out.append(bold("#" + str(i)) + "  " + cyan(_cite(h)) + "   " + dim(tag))
        if show:
            for line in (h.get("body") or "").splitlines():
                out.append("    " + line)
        else:
            out.append("    " + (h.get("snip") or ""))
        out.append(dim("    → " + str(h.get("kb_path"))))
        out.append("")
    return "\n".join(out).rstrip()


def _render_json(hits: list[dict[str, Any]], show: bool) -> str:
    payload = []
    for h in hits:
        rec = {
            "doc_id": h.get("doc_id"),
            "source_path": h.get("source_path"),
            "kb_path": h.get("kb_path"),
            "source_type": h.get("source_type"),
            "page": h.get("page"),
            "heading": h.get("heading"),
            "citation": _cite(h),
            "score": h.get("score"),
            "snippet": h.get("snip"),
        }
        if show:
            rec["text"] = h.get("body")
        payload.append(rec)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _print_info(conn: sqlite3.Connection, kb_dir: Path) -> int:
    meta = _meta(conn)
    n_docs = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    backend = "fts5" if _has_fts(conn) else "python-bm25 (FTS5 unavailable)"
    print(json.dumps({
        "kb_dir": str(kb_dir),
        "index": str(kb_dir / INDEX_DB_NAME),
        "backend": backend,
        "analyzer": meta.get("analyzer") or "none (legacy index: unstemmed; rebuild to stem)",
        "documents": n_docs,
        "chunks": n_chunks,
        "indexer": meta.get("indexer"),
        "created_at": meta.get("created_at"),
        "chunk_target_tokens": meta.get("chunk_target_tokens"),
        "corpus_signature": meta.get("corpus_signature"),
    }, ensure_ascii=False, indent=2))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="query_kb",
        description="Citation-first BM25 search over a doc2kb knowledge base.",
    )
    ap.add_argument("kb_dir")
    ap.add_argument("query", nargs="?", default=None)
    ap.add_argument("-k", "--top-k", type=int, default=8)
    ap.add_argument("--doc", default=None)
    ap.add_argument("--type", dest="stype", default=None)
    ap.add_argument("--page", type=int, default=None)
    ap.add_argument("--and", dest="require_all", action="store_true")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--json", dest="as_json", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--info", action="store_true", help="print index stats and exit")
    args = ap.parse_args(argv)

    kb_dir = Path(args.kb_dir).expanduser().resolve()
    try:
        conn = _open_db(kb_dir)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2

    try:
        if args.info:
            return _print_info(conn, kb_dir)
        if not args.query:
            print("usage: query_kb.py <kb_dir> \"<question>\"  (or --info)",
                  file=sys.stderr)
            return 2
        try:
            if not _is_stemmed(conn):
                print("note: this index predates doc2kb's Russian/English stemming — "
                      "inflected forms won't match; rebuild it: index_kb.py "
                      f"{kb_dir} (or `dkb index`)", file=sys.stderr)
            if _has_fts(conn):
                hits = _search_fts(
                    conn, args.query, top_k=args.top_k, doc=args.doc,
                    stype=args.stype, page=args.page, raw=args.raw,
                    require_all=args.require_all)
            else:
                hits = _search_bm25(
                    conn, args.query, top_k=args.top_k, doc=args.doc,
                    stype=args.stype, page=args.page, require_all=args.require_all)
        except sqlite3.OperationalError as e:
            print(f"query error: {e}", file=sys.stderr)
            return 2

        if args.as_json:
            print(_render_json(hits, args.show))
        else:
            color = sys.stdout.isatty() and not args.no_color
            print(_render_human(hits, args.show, color))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
