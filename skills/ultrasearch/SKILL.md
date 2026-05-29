---
name: ultrasearch
description: Thesis-level literature research (систематический обзор, ВКР, related work) on a topic: queries 6 open academic APIs (OpenAlex, Semantic Scholar, arXiv, Crossref, Europe PMC, CORE), downloads OA PDFs via a multi-tier cascade, builds a persistent SPECTER-embedded SQLite corpus with PaperQA2-style citation traversal, flags retractions, and emits a markdown report with DOI-grounded citations and optional BibTeX/Zotero export. MUST BE USED whenever the user asks for a literature review, related work, deep research, систематический обзор, ВКР, дипломная работа, "найди все статьи про X", thesis prep, or research synthesis. NOT for single-paper summarization (use the `pdf` skill), code documentation (use repomix/gitingest), or general web research without academic grounding.
---

# ultrasearch — thesis-level literature research

## ⛔ Rules that override everything else

1. **NEVER fabricate DOIs or citations.** Every DOI in the final report MUST exist in `papers.doi` of `corpus.db`. A regex-validated check is in the Checklist section; run it before delivering output.
2. **NEVER skip the env-var pre-flight.** `OPENALEX_API_KEY` and `UNPAYWALL_EMAIL` are not optional. Failing fast on missing env vars is correct behavior, not a bug.
3. **NEVER bypass the venv.** Every script is invoked through `ensure_env.py`, which resolves the skill's venv (a global state dir outside the code — ADR-008). Calling extract/index scripts with system `python3` will fail — `sentence-transformers`, `pymupdf4llm`, and `sqlite-vec` live only in that venv.
4. **NEVER deliver a report whose paragraphs lack `[Sn]` citation markers.** `synthesize.validate_report()` returns the offenders — fix or remove them.
5. **NEVER delete `corpus.db`.** It is user content (ADR-008), stored in a global state dir outside the code; reinstall never touches it. If the user explicitly asks to reset, resolve the path as in *Persistent corpus* and `rm "$DB"*`.

## Required environment variables

```bash
export OPENALEX_API_KEY="..."    # free key — https://openalex.org/account
export OPENALEX_EMAIL="you@example.com"   # polite-pool identifier
export UNPAYWALL_EMAIL="you@example.com"  # mandatory for Tier 2 fetch
export S2_API_KEY="..."           # optional — dedicated 1 RPS pool
# Stage 2 additions:
export CORE_API_KEY="..."         # optional — enables CORE source (5 of 6 work without)
export ZOTERO_API_KEY="..."       # optional — enables --export-zotero remote push
export ZOTERO_USER_ID="..."       # required if ZOTERO_API_KEY set
```

| Var | Required? | What breaks without it |
|---|---|---|
| `OPENALEX_API_KEY` | YES (post 13 Feb 2026) | OpenAlex returns 401 for new users |
| `OPENALEX_EMAIL` | Recommended | drops to anonymous quota |
| `UNPAYWALL_EMAIL` | YES | Tier 2 (Unpaywall PDF lookup) skipped; cascade loses ~30% recall |
| `S2_API_KEY` | Optional | shares anonymous 5000/5min bucket; traversal hits it harder |
| `CORE_API_KEY` | Optional (Stage 2) | CORE source disabled; other 5 sources cover |
| `ZOTERO_API_KEY` | Optional (Stage 2) | `--export-zotero` writes `.bib` only, no remote push |
| `ZOTERO_USER_ID` | Required if ZOTERO_API_KEY set | API push fails fast |

Pre-flight one-liner:

```bash
for v in OPENALEX_API_KEY OPENALEX_EMAIL UNPAYWALL_EMAIL S2_API_KEY; do
    printf "%-22s %s\n" "$v" "${!v:+set}"
done
```

## When to use

Triggers (any language):

- "literature review on X", "найди все статьи про X"
- "related work for my thesis / paper / RFC"
- "систематический обзор по теме X", "ВКР по нейроинтерфейсам"
- "what do papers say about X / какие исследования есть по теме Y"
- "thesis prep", "обзор источников", "deep research on X"

NOT for:

- single-paper summarization → use Anthropic's `pdf` skill
- generating new papers/code from a topic → use `gost-report` (RU) / your own writing
- code documentation → use `repomix` / `gitingest`
- general web research without academic grounding → use `WebSearch` / `WebFetch` directly

## Canonical invocation

The skill is invoked through `ensure_env.py` so dependencies install into a local venv on first call (~3-5 min cold, <30 ms warm).

```bash
python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py ultrasearch.py \
    "SSVEP-based BCIs neural prosthetics" \
    --max-papers 30 \
    --out /tmp/ssvep-review.md
```

### CLI flags

| Flag | Default | Purpose |
|---|---|---|
| `query` (positional) | required | research topic, quoted, multilingual |
| `--max-papers N` | 30 | cap on papers fetched + parsed + indexed |
| `--max-per-source N` | 50 | cap per discovery source before dedup |
| `--out PATH` | stdout | write report to file |
| `--top-k N` | 20 | chunks for synthesis |
| `--no-fetch` | off | skip PDF download; index abstracts only |
| `--no-synthesize` | off | emit pipeline JSON instead of markdown |
| `--db PATH` | global `…/agentpipe/ultrasearch/data/corpus.db` (see *Persistent corpus*) | override corpus location |
| `--json` | off | emit pipeline statistics on stderr |
| `--quiet` | off | suppress per-step log lines |
| `--depth shallow\|default\|deep` | default | Stage 2: shallow=no traversal; default=1 hop; deep=2 hops |
| `--export-zotero` | off | Stage 2: write .bib + push to Zotero (if env vars set) |
| `--bib-out PATH` | `<out>.bib` | Stage 2: path for .bib file |
| `--refresh-retractions` | off | Stage 2: pull latest Retraction Watch CSV |
| `--no-docling` | off | Stage 2: disable docling fallback (pymupdf4llm only) |
| `--sources LIST` | all 8 | comma-separated subset of {openalex,s2,arxiv,crossref,europepmc,core,datacite,hf} |
| `--profile NAME` | `academic` | v2: pipeline profile (`auto`, `academic`, `dev`, `docs`). `auto` = classifier picks. `academic` = v1 pipeline (default). Other profiles use the generic flat orchestrator. |
| `--retrieval-profile fast\|full` | fast | Renamed from v1 `--profile fast/full`. Old `--profile=fast`/`--profile=full` still works via deprecation shim. `full` = cross-encoder rerank + RCS-cached scoring + (planned) multi-section synth |
| `--profiles-dir PATH` | `<skill>/profiles` | override profile YAML directory |
| `--no-classifier` | off | skip classifier; require explicit `--profile` |
| `--output-template NAME` | (profile default) | force a template (`library_matrix.md.j2`, `adr.md.j2`, ...) |
| `--lang auto\|en\|ru` | auto | Stage 3: query-language detection + EN parallel for non-EN queries (argos-translate) |
| `--render-graph` | off | Stage 3: embed Mermaid citation graph in the report |
| `--grey` | off | Stage 3: sci-hub opt-in fallback (LEGAL GRAY ZONE — see disclaimer) |

## v2 profiles (v0.15+)

v2 introduces profile-routed pipelines. The `academic` profile (default) is the v1 pipeline below, byte-identical. Other profiles use a generic flat orchestrator (`orchestrate.py`) over source clients in `scripts/sources/`.

| Profile | Sources | Output template | Status |
|---|---|---|---|
| `academic` | OpenAlex, S2, arXiv, Crossref, EuropePMC, CORE | `literature_review.md.j2` | v1 (full, ships) |
| `dev` | GitHub, Stack Exchange, HN Algolia, deps.dev, PyPI, docs_crawl | `library_matrix.md.j2` (alt: `adr.md.j2`) | Stage 1 (ships) |
| `docs` | llms.txt → sitemap → Crawl4AI BFS → trafilatura | `literature_review.md.j2` | Stage 1 (ships; crawl4ai lazy-installed on first use) |
| `auto` | (classifier picks) | (per primary profile) | Stage 1 ships keyword fallback; subagent integration is Stage 1.5 |

**Query style by profile — this matters.** Only the `academic` profile does semantic (SPECTER-embedded) matching and handles full natural-language topics. The `dev` profile hits GitHub repo search, Stack Exchange, HN Algolia, and package registries — engines that match **short keyword / library-name** queries, *not* prose. Feed it terse terms or a tool name:

- ✅ `"rust async runtime"`, `"pdf table extraction python"`, `"BFG git secrets"`, `redis`
- ❌ `"what are the best practices for safely cleaning up a git repository and removing stale artifacts"` → returns ~zero hits; these APIs don't do sentence-level semantic search, and the output is a library-comparison matrix, not a how-to.

So reach for `dev` when the question is **"which library/tool for X"**. For how-to / best-practice questions, use a few keywords, the `docs` profile against known doc roots, or plain `WebSearch` / `WebFetch`. The `auto` classifier can still mis-route a long prose query to `dev`; pass `--profile` explicitly (or shorten the query) when you know you want tool discovery.

**Routing logic**: `ultrasearch.py` resolves `--profile`:
- omitted / `academic` / legacy `fast|full` → v1 `_pipeline()` (academic)
- `auto` → classifier (keyword fallback for now) → picks primary profile
- named profile with YAML → v2 `orchestrate.run_pipeline(profile)`
- named profile without YAML (e.g. `--profile=regulatory` in Stage 1) → falls back to academic with a log line

**Customizing**: drop a `<name>.yaml` into `<skill>/profiles/`, validated against `profiles/_schema.json`. Sources reference modules in `scripts/sources/<id>.py`. Scoring is a `scoring/<file>.py:<func>` reference. Output template is a Jinja2 file in `templates/`.

**SSRF / XXE hardening**: `crawl/__init__.py:_url_is_safe()` rejects loopback / private / link-local / cloud-metadata IPs; `follow_redirects=False`; sitemap parsing uses regex (no XML parser). Source-client HTTP error logs sanitize URLs (`type(e).__name__` only).

### v2 example invocations

```bash
# dev profile (force, skip classifier)
python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py ultrasearch.py \
    "best Python web framework 2026" \
    --profile=dev --max-papers 25 --top-k 8 \
    --out /tmp/web-frameworks.md

# auto-classify, pure-academic query routes to v1 pipeline unchanged
python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py ultrasearch.py \
    "literature review on SSVEP BCI" \
    --profile=auto --max-papers 30

# docs profile with explicit root URLs (avoid query-only URL extraction)
python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py orchestrate.py \
    "API reference" --profile=docs \
    --max-items 50 --top-k 20 --out /tmp/docs.md
# Requires: ensure_env.py runs ensure_crawl4ai() to lazy-install Playwright
```

## Pipeline (academic profile — v1 / Stage 2)

```
discover (async, 6 sources concurrent)
   ├─ OpenAlex    /works              ← abstracts + referenced_works (traversal seed)
   ├─ Semantic Scholar /paper/search   ← citation_count, externalIds
   ├─ arXiv       /api/query           ← preprints + bare-ID lookup
   ├─ Crossref    /works               ← Stage 2 — DOI metadata + backward refs
   ├─ Europe PMC  /search              ← Stage 2 — biomed full-text manifests
   └─ CORE        /v3/search/works     ← Stage 2 — 449M OA articles
       ↓
   dedup by normalize_doi(doi) or 'T:' + casefold_title(title)
       ↓
   fetch (async, sem=4, 7-tier cascade)
   ├─ Tier 1: OpenAlex primary_location.pdf_url
   ├─ Tier 2: Unpaywall best_oa_location.url_for_pdf
   ├─ Tier 3: arXiv direct PDF
   ├─ Tier 4: Europe PMC PMC fulltext
   ├─ Tier 5: publisher OA templates (PLOS, Frontiers)
   ├─ Tier 6: Zenodo records
   └─ Tier 7: arXiv HTML (ar5iv)  ← Stage 3 hook
       ↓
   parse (subprocess per PDF, pymupdf4llm + docling fallback)
   ├─ default: pymupdf4llm only
   └─ per-page heuristic → docling re-parses table/math pages, stitch back in
       ↓
   index (serial, BEGIN IMMEDIATE writes)
   ├─ chunk_text → ~500-token paragraph-aware windows
   ├─ allenai-specter encode (MPS on Apple Silicon, ~50 chunks/s)
   └─ vec_chunks INSERT (rowid == chunks.chunk_id invariant)
       ↓
   retrieve (top-20 via sqlite-vec MATCH)
       ↓
   traverse (Stage 2 — PaperQA2 Algorithm 1)
   ├─ seeds = top-10 hits with score ≥ 0.8
   ├─ 3 API calls per seed: S2 refs + Crossref refs + S2 citers
   ├─ SPECTER cosine gate against corpus centroid (≥ 0.55)
   └─ overlap filter + ell=12 cap → list of new candidates
       ↓
   quality (Stage 2 — Retraction Watch join)
   ├─ optional --refresh-retractions: pull CSV from gitlab.com/crossref/retraction-watch-data
   └─ mark_retractions: UPDATE papers.retracted_at on DOI match
       ↓
   synthesize → markdown with [S1]..[Sn] markers
   ├─ ⚠ RETRACTED footnotes on flagged refs
   ├─ Open Questions section pulled from traversal candidates
   └─ validate_report: every paragraph has [Sn]; every [Sn] in References
       ↓
   (optional) export → .bib via bibtexparser + Zotero push via pyzotero
```

## First-run cost

- `torch` wheel ~150 MB on macOS arm64
- `sentence-transformers` + transformers ~80 MB
- `allenai-specter` model ~440 MB (downloaded on first `index.py` call, NOT during pip install)
- `docling` model bundle ~358 MB (downloaded on first docling fallback trigger, NOT during pip install) — Stage 2
- `git clone` of Retraction Watch repo ~50 MB (Stage 2 — only if `--refresh-retractions`)
- Cold install: 5-8 minutes
- Warm second run: <30 ms bootstrap; 30-90 s for a fresh query depending on PDF availability and `--max-papers`

## Persistent corpus

The corpus lives in a **global state dir outside the installed code** (ADR-008), keyed by skill name so every install target (`~/.claude`, `~/.codex`) shares one. Reinstall / update / multi-target install **never** touch it. Resolve the path:

```bash
DB="${ULTRASEARCH_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/agentpipe/ultrasearch}/data/corpus.db"
```

- Backup: `cp "$DB" /backup/corpus-$(date +%Y%m%d).db`
- Reset: `rm "$DB"*` — next invocation rebuilds from `schema.sql`
- Inspect: `sqlite3 "$DB" ".tables"` — should list `papers, chunks, vec_chunks, citations, retractions, schema_meta`

## Output format

Markdown report with:

```
# <query> — research brief

## TL;DR
<stitched abstract using top-3 hits, each citing [S1] [S2] [S3]>

## Findings
### <paper title> (<year>) — <venue>
<truncated chunk text>... [S1]

### ...

## Open questions / related directions
- *<title>* — [S9]

## References
- [S1] 10.xxxx/yyyy — *<title>*, <authors>, <venue>, <year>.
...
```

## What ships in v1.0 (Stages 1+2+3)

**Stage 1 (MVP):** OpenAlex + Semantic Scholar + arXiv discovery, Unpaywall + arXiv 2-tier fetch, pymupdf4llm parsing, SPECTER embedding in sqlite-vec, deterministic markdown report with `[Sn]` citations.

**Stage 2 (v0.5):** Crossref/Europe PMC/CORE added (6 sources total), 7-tier PDF cascade with publisher OA templates and Zenodo, docling fallback on math/table-heavy pages, PaperQA2 Algorithm 1 citation traversal (`--depth`), Retraction Watch CSV ingest + DOI join (`--refresh-retractions`), BibTeX/Zotero export (`--export-zotero`), Open Questions section in synthesis.

**Stage 3 (v1.0):**
- 3-stage retrieval (`--profile full`): sqlite-vec top-50 → bge-reranker top-10 → RCS-cached scoring (≥5 keeps)
- Multilingual query expansion (`--lang auto`): argos-translate EN↔RU; abstract translations stored in `papers.abstract_translated`
- Composite Q-score: `0.3·log(cit+1) + 0.2·venue + 0.2·h_index + 0.3·recency` (populated lazily by `quality.populate_q_scores`)
- MMR diversity selection (λ=0.7) + institution≤25% / primary-author≤15% caps in `quality.apply_diversity_caps`
- Mermaid citation graph rendering (`--render-graph`, capped at 50 nodes)
- DataCite + HuggingFace Papers added to discovery; КиберЛенинка / OATD / BASE stubs in place for v1.1
- CriticAgent prompt template at `prompts/critic.txt` (5 issue types, severity rubric; orchestrator-side validation loop in roadmap)
- Sci-hub `--grey` opt-in tier 8 with explicit disclaimer (legal gray zone — research §14 line 300)
- New tables: `rcs_scores` (PaperQA2 RCS score cache keyed by `sha256(query)[:16]`)

## Known deferrals (post-v1.0)

- specter2 adapter framework / multilingual-e5-large / mlx-embeddings backend (alt embedders)
- PostToolUse hook for auto-indexing PDFs you Read (per research §13 line 289)
- CriticAgent loop integration into `synthesize.py` (the prompt is shipped; the orchestrator currently uses deterministic synth — wrapping the critic into a retry loop is Stage 3.1)
- LanceDB migration when corpus crosses ~500k chunks
- mlx-lm offline LLM for RCS scoring (research §11 line 240)
- BEIR-SciFact / TREC-COVID benchmark suite (research §recommendations item 5)
- `/ultrasearch-update` slash command for periodic Retraction Watch + corpus refresh
- КиберЛенинка / OATD / BASE actual scrapers (stubs registered; OAI-PMH parsing pending)

## References (lazy-loaded)

- `references/apis.md` — endpoint specs, rate budgets, env vars (all 11 sources)
- `references/parsing-troubleshooting.md` — pymupdf4llm AGPL, MPS device, docling fallback notes
- `references/methodology.md` — Wohlin 2014 snowballing + PaperQA2 Algorithm 1 + SPECTER cosine gate
- `references/ru-sources.md` — КиберЛенинка status, eLIBRARY exclusion, РГБ note, translation flow (Stage 3)
- `prompts/perspective-questions.txt` — STORM-style perspective generation (Stage 2)
- `prompts/rcs-summary.txt` — PaperQA2 RCS chunk-scorer prompt (Stage 3)
- `prompts/section-writer.txt` — section-draft subagent prompt (Stage 3)
- `prompts/polisher.txt` — final stitch + TL;DR + References writer prompt (Stage 3)
- `prompts/critic.txt` — CriticAgent integrity-gate prompt (Stage 3)

## Dependencies

`scripts/requirements.txt`: pyalex, semanticscholar, arxiv, httpx[http2], aiometer, tenacity, unpywall, pymupdf4llm, torch, sentence-transformers, sqlite-vec, numpy, tiktoken. No system deps beyond Python 3.10+.

**AGPL note:** `pymupdf4llm` and its transitive `pymupdf` are AGPL v3 (Artifex). Fine for an open-source skill; commercial reuse needs a license from Artifex. See `references/parsing-troubleshooting.md`.

## License

See LICENSE — MIT.

## Checklist (before delivering output)

1. Report is non-empty: `wc -c <report>` > 1000
2. Every paragraph cites at least one `[Sn]` marker:
   ```bash
   python3 -c "import re,sys; t=open('$REPORT').read(); body=t.split('## References',1)[0]; paras=[p for p in re.split(r'\\n\\s*\\n', body) if p.strip() and not p.lstrip().startswith(('#','>','-','*','|','\`'))]; bad=[p for p in paras if '[S' not in p]; print('bad paragraphs:', len(bad))"
   ```
3. Every `[Sn]` resolves to a References entry:
   ```bash
   python3 -c "import re; t=open('$REPORT').read(); body,refs=t.split('## References',1) if '## References' in t else (t,''); a=set(re.findall(r'\\[(S\\d+)\\]',body)); b=set(re.findall(r'\\[(S\\d+)\\]',refs)); print('missing in refs:', sorted(a-b))"
   ```
4. No fabricated DOIs:
   ```bash
   sqlite3 "${ULTRASEARCH_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/agentpipe/ultrasearch}/data/corpus.db" <<SQL
   .mode list
   SELECT count(*) FROM papers WHERE doi IS NOT NULL;
   SQL
   # Cross-check with: grep -oE '10\\.\\d{4,}/[^ )\\]]+' "$REPORT" | sort -u
   # Every DOI in the report must appear in papers.doi.
   ```
5. `corpus.db` grew by ≥1 paper this invocation (`schema_meta.last_indexed_at` advanced, or row counts increased).
