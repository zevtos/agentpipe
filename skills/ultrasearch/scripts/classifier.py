"""
classifier.py — route a research query to ultrasearch profile weights.

Stage 1 ships **two** classifier paths:

1. **Subagent** (primary in production) — driven by `classifier.md` at the
   skill root. The parent LLM session reads classifier.md, runs the Task
   tool with context: fork, captures the JSON, and writes it to
   `$ULTRASEARCH_CLASSIFIER_RESULT` before invoking this module. Python
   then reads that file and validates it.

2. **Keyword fallback** (always available) — deterministic rule-based
   routing from query keywords. Used when:
   - `--no-classifier` was passed
   - the subagent result file is missing
   - the subagent JSON is malformed or fails validation
   - running under CI / scripted eval without a parent LLM

`forced_profile` short-circuits both paths to a single profile at weight 1.0.

The function `classify()` **never raises**. On any error, it returns a
sane default (`academic`, confidence 0.0) so the rest of the pipeline can
proceed.

CLI: `python3 classifier.py "some query"` — useful for debugging the
keyword fallback in isolation.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from _common import log


# Eight canonical profile labels. The classifier always emits all eight,
# even if most are 0.0 — downstream code expects every key present.
PROFILE_KEYS: tuple[str, ...] = (
    "academic", "dev", "product", "startup",
    "regulatory", "docs", "community", "meta",
)

ALLOWED_OUTPUT_FORMATS: tuple[str, ...] = (
    "literature_review", "library_matrix", "adr", "kb_index", "source_list",
)


@dataclass(frozen=True)
class ClassifierResult:
    """Routing decision returned by classify()."""
    profiles: dict[str, float]              # 8 keys, sum to 1.0
    is_recursive: bool
    branching_keys: tuple[str, ...]
    output_format: str
    confidence: float
    clarifications_needed: tuple[dict, ...]  # each: {"q": ..., "why": ...}
    primary_profile: str                     # argmax of profiles
    source: str                              # 'subagent' | 'keyword_fallback' | 'forced'


# ---------- result construction helpers ----------

def _zero_profiles() -> dict[str, float]:
    return {k: 0.0 for k in PROFILE_KEYS}


def _normalize_profiles(p: dict[str, float]) -> dict[str, float]:
    """Ensure all 8 keys present, clip to [0, 1], renormalize to sum 1.0.
    If the sum is 0 (everything zero), default to academic=1.0."""
    out = _zero_profiles()
    for k, v in (p or {}).items():
        if k in out:
            try:
                out[k] = max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                out[k] = 0.0
    total = sum(out.values())
    if total <= 0:
        out["academic"] = 1.0
        return out
    if abs(total - 1.0) > 0.01:
        for k in out:
            out[k] = out[k] / total
    return out


def _primary(profiles: dict[str, float]) -> str:
    return max(profiles.items(), key=lambda kv: kv[1])[0]


def _result_from_json(data: dict, *, source: str) -> ClassifierResult:
    """Build a ClassifierResult from a (possibly partial) JSON dict.
    Robust to missing fields — fills sensible defaults."""
    profiles = _normalize_profiles(data.get("profiles") or {})
    is_recursive = bool(data.get("is_recursive", False))
    branching_keys = tuple(data.get("branching_keys") or [])
    if not is_recursive:
        branching_keys = ()
    output_format = data.get("output_format") or "literature_review"
    if output_format not in ALLOWED_OUTPUT_FORMATS:
        output_format = "literature_review"
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0

    raw_clar = data.get("clarifications_needed") or []
    clarifications: list[dict] = []
    if isinstance(raw_clar, list):
        for entry in raw_clar[:1]:  # cap at 1
            if isinstance(entry, dict) and "q" in entry:
                clarifications.append({
                    "q": str(entry.get("q", "")),
                    "why": str(entry.get("why", "")),
                })

    return ClassifierResult(
        profiles=profiles,
        is_recursive=is_recursive,
        branching_keys=branching_keys,
        output_format=output_format,
        confidence=confidence,
        clarifications_needed=tuple(clarifications),
        primary_profile=_primary(profiles),
        source=source,
    )


def _academic_default(source: str = "keyword_fallback") -> ClassifierResult:
    """Safe fallback when everything else fails."""
    profiles = _zero_profiles()
    profiles["academic"] = 1.0
    return ClassifierResult(
        profiles=profiles,
        is_recursive=False,
        branching_keys=(),
        output_format="literature_review",
        confidence=0.0,
        clarifications_needed=(),
        primary_profile="academic",
        source=source,
    )


# ---------- subagent hand-off ----------

def _subagent_call(query: str) -> Optional[ClassifierResult]:
    """STUB for Stage 1.

    `classifier.md` is consumed by the calling LLM session via the Task tool
    (context: fork). That tool is a runtime primitive accessible only to the
    LLM driving Claude Code / Codex — not to this Python module. So we cannot
    invoke the subagent from Python directly.

    The integration contract is **file-based**: the parent LLM writes the
    classifier JSON to the path in $ULTRASEARCH_CLASSIFIER_RESULT, then
    invokes this script. We read and validate it.

    If no result file is present (CLI invocation, eval pipeline, etc.) we
    return None so the caller falls back to the keyword classifier.

    Stage 1.5 will wire this end-to-end via a small `dispatch` helper in
    ultrasearch.py that runs the Task tool and writes the result file.
    """
    handoff_path = os.environ.get("ULTRASEARCH_CLASSIFIER_RESULT")
    if not handoff_path:
        return None
    if not os.path.isfile(handoff_path):
        log(f"classifier hand-off file not found: {handoff_path}")
        return None
    # Refuse special files and oversize JSON (1 MB cap — classifier results
    # are normally < 2 KB).
    try:
        st = os.stat(handoff_path)
    except OSError as e:
        log(f"classifier hand-off stat failed: {type(e).__name__}")
        return None
    import stat as _stat
    if not _stat.S_ISREG(st.st_mode):
        log(f"classifier hand-off not a regular file: {handoff_path}")
        return None
    if st.st_size > 1_048_576:
        log(f"classifier hand-off too large ({st.st_size} bytes); ignoring")
        return None
    try:
        with open(handoff_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        log(f"failed to read classifier hand-off: {type(e).__name__}")
        return None
    if not isinstance(data, dict) or "profiles" not in data:
        log(f"classifier hand-off malformed (no 'profiles' key): {handoff_path}")
        return None
    log(f"loaded classifier result from {handoff_path}")
    return _result_from_json(data, source="subagent")


# ---------- keyword fallback ----------

# Each tuple is (pattern, profile_label, weight). Patterns are compiled
# case-insensitively. Profiles accumulate weights; the result is renormalized.
# Patterns are deliberately broad — false positives are fine because
# everything renormalizes.
_KEYWORD_RULES: tuple[tuple[re.Pattern, str, float], ...] = (
    # academic
    (re.compile(r"\b(paper|papers|review|related work|thesis|study|studies|research|survey|meta[- ]analysis|peer[- ]reviewed|citation|literature)\b", re.I), "academic", 1.0),
    (re.compile(r"\b(researchgate|pubmed|arxiv|crossref|openalex|semantic scholar|google scholar|ieee|acm)\b", re.I), "academic", 1.2),
    (re.compile(r"(вкр|свкр|диплом|курсов|магистерск|систематическ|обзор|научн)", re.I), "academic", 1.0),

    # dev
    (re.compile(r"\b(framework|library|libraries|package|packages|sdk|api client|cli|tool|tools|best|vs\.?|comparison|alternatives?)\b", re.I), "dev", 1.0),
    (re.compile(r"\b(github|stack ?overflow|hacker ?news|install|build|architecture|stack|tech stack|python|rust|go|java|kotlin|node|deno|bun)\b", re.I), "dev", 0.8),
    (re.compile(r"\b(should i use|how to choose|implement|implementation|deploy|deployment)\b", re.I), "dev", 0.6),

    # docs
    (re.compile(r"\b(documentation|docs|how to use|integrate|integration|api reference|tutorial|guide|onboarding|getting started)\b", re.I), "docs", 1.0),
    (re.compile(r"\b(stripe api|twilio|telegram bot|webhook|endpoint|payload schema)\b", re.I), "docs", 0.8),

    # startup
    (re.compile(r"\b(startup|yc|y combinator|funding|seed|series [abc]|competitor|market|business model|gtm|go[- ]to[- ]market|valuation|investor|pitch)\b", re.I), "startup", 1.0),

    # community
    (re.compile(r"\b(discussion|opinion|reddit|community|people say|forum|thread|hn comments|hacker ?news|lobste\.rs)\b", re.I), "community", 1.0),

    # regulatory
    (re.compile(r"\b(regulation|law|legal|compliance|gdpr|hipaa|sox|court|ruling|jurisdiction|statute|directive|policy)\b", re.I), "regulatory", 1.0),

    # product
    (re.compile(r"\b(product|feature|features|ux|ui design|design pattern|launch|roadmap|customer|user research|persona)\b", re.I), "product", 1.0),

    # meta
    (re.compile(r"\b(skill|skills|subagent|sub-agent|claude code|claude\.md|mcp|model context protocol|meta|agentic|tool use)\b", re.I), "meta", 1.0),
)

# Recursion-trigger patterns. If any match, is_recursive=True and
# branching_keys is filled from the matching group(s).
_RECURSION_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(all|every|each)\s+(\w+)s\b", re.I), "noun"),       # "all networks", "every framework"
    (re.compile(r"\b(supporting|across)\s+(all|every|each)\s+(\w+)", re.I), "noun"),
    (re.compile(r"\b(cross[- ]?platform|multi[- ]?platform|multi[- ]?cloud|multi[- ]?chain|multi[- ]?language)\b", re.I), "platform"),
    (re.compile(r"\b(for|across)\s+(EVM|Solana|Cosmos|Bitcoin|TON|Tron|Polkadot|NEAR|Aptos|Sui)\b", re.I), "network"),
)


def _detect_recursion(query: str) -> tuple[bool, tuple[str, ...]]:
    keys: list[str] = []
    is_recursive = False
    for pattern, label in _RECURSION_RULES:
        m = pattern.search(query)
        if not m:
            continue
        is_recursive = True
        # Extract a plausible branching key. For "all networks" → "network".
        if label == "noun":
            # last group is the plural noun; strip trailing 's' for the key.
            noun = m.groups()[-1] if m.groups() else ""
            if noun:
                key = noun.lower().rstrip("s")
                if key and key not in keys:
                    keys.append(key)
        elif label == "platform":
            if "platform" not in keys:
                keys.append("platform")
        elif label == "network":
            if "network" not in keys:
                keys.append("network")
    return is_recursive, tuple(keys[:3])  # cap at 3 branching dimensions


def _pick_output_format(primary: str, is_recursive: bool) -> str:
    if is_recursive:
        return "kb_index"
    if primary == "academic":
        return "literature_review"
    if primary in ("dev", "docs", "product"):
        return "library_matrix"
    if primary == "startup":
        return "library_matrix"  # no dedicated startup template in Stage 1
    if primary in ("regulatory", "community", "meta"):
        return "source_list"
    return "literature_review"


def _keyword_fallback(query: str) -> ClassifierResult:
    """Deterministic rule-based classifier. Always succeeds."""
    scores: dict[str, float] = {k: 0.0 for k in PROFILE_KEYS}
    for pattern, label, weight in _KEYWORD_RULES:
        if pattern.search(query):
            scores[label] += weight

    profiles = _normalize_profiles(scores)
    is_recursive, branching_keys = _detect_recursion(query)
    primary = _primary(profiles)
    output_format = _pick_output_format(primary, is_recursive)

    max_w = max(profiles.values()) if profiles else 0.0
    confidence = min(1.0, max_w + 0.1)  # mild boost; max ~1.0
    if max_w < 0.4:
        confidence = max_w  # stay honest about uncertainty

    clarifications: tuple[dict, ...] = ()
    if max_w < 0.4:
        clarifications = ({
            "q": "Could you specify whether you want academic papers, library/tool recommendations, or product/market research?",
            "why": "no single profile dominates the keyword routing",
        },)

    return ClassifierResult(
        profiles=profiles,
        is_recursive=is_recursive,
        branching_keys=branching_keys,
        output_format=output_format,
        confidence=confidence,
        clarifications_needed=clarifications,
        primary_profile=primary,
        source="keyword_fallback",
    )


# ---------- public API ----------

def classify(
    query: str,
    *,
    no_classifier: bool = False,
    forced_profile: Optional[str] = None,
) -> ClassifierResult:
    """Route a query to profile weights.

    Resolution order:
    1. `forced_profile` set → return weight 1.0 on that profile, source='forced'.
    2. `no_classifier=True` → keyword fallback only.
    3. Try subagent hand-off (reads $ULTRASEARCH_CLASSIFIER_RESULT file).
    4. Fall back to keyword classifier.

    Never raises. On any error, returns academic default with confidence 0.0.
    """
    try:
        if forced_profile:
            if forced_profile not in PROFILE_KEYS:
                log(f"forced_profile {forced_profile!r} not in canonical set; using academic")
                forced_profile = "academic"
            profiles = _zero_profiles()
            profiles[forced_profile] = 1.0
            return ClassifierResult(
                profiles=profiles,
                is_recursive=False,
                branching_keys=(),
                output_format=_pick_output_format(forced_profile, False),
                confidence=1.0,
                clarifications_needed=(),
                primary_profile=forced_profile,
                source="forced",
            )

        if not query or not query.strip():
            log("classify(): empty query, returning academic default")
            return _academic_default()

        if no_classifier:
            return _keyword_fallback(query)

        sub = _subagent_call(query)
        if sub is not None:
            return sub

        return _keyword_fallback(query)
    except Exception as e:
        log(f"classify() unexpected error: {e!r}; using academic default")
        return _academic_default()


# ---------- CLI ----------

def _result_to_jsonable(r: ClassifierResult) -> dict:
    return {
        "profiles": r.profiles,
        "is_recursive": r.is_recursive,
        "branching_keys": list(r.branching_keys),
        "output_format": r.output_format,
        "confidence": r.confidence,
        "clarifications_needed": [dict(c) for c in r.clarifications_needed],
        "primary_profile": r.primary_profile,
        "source": r.source,
    }


if __name__ == "__main__":
    import sys
    query = sys.argv[1] if len(sys.argv) > 1 else "literature review on SSVEP BCI"
    no_cls = "--no-classifier" in sys.argv[2:]
    forced = None
    for arg in sys.argv[2:]:
        if arg.startswith("--profile="):
            forced = arg.split("=", 1)[1]
    result = classify(query, no_classifier=no_cls, forced_profile=forced)
    print(json.dumps(_result_to_jsonable(result), indent=2, ensure_ascii=False))
