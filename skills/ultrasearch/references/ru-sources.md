# Russian-language academic sources (Stage 3)

Stage 3 adds opportunistic coverage of Russian-language literature. The skill
**does NOT** rely on these as primary sources — they supplement the main
Western indexes (OpenAlex, S2, arXiv, Crossref) which already cover the
majority of citable RU work that has a DOI.

## КиберЛенинка (cyberleninka.ru)

**Status:** not wired. The earlier `_query_kiberleninka()` stub in
`discover.py` was deleted (it returned `[]` unconditionally and only
inflated the dispatch chain). A future Stage 3.1 implementation should
re-add a real OAI-PMH client.

КиберЛенинка is the largest open RU-language repository (~3M articles). It
exposes an undocumented OAI-PMH endpoint at `cyberleninka.ru/oai` — operational
but not formally supported. The user-friendly approach:

1. Most RU papers with citations elsewhere have DOIs (assigned by RSCI / their
   journal). Those DOIs surface via OpenAlex and Crossref already.
2. For RU papers without DOIs, КиберЛенинка's HTML pages are scrape-friendly
   (predictable selectors, no JS), but the legal status of bulk scraping is
   ambiguous — the site's robots.txt does not forbid it.
3. A future Stage 3.1 implementation should use `requests-html` or `trafilatura`
   against the OAI-PMH endpoint with `verb=ListRecords&metadataPrefix=oai_dc`,
   parsing the Dublin Core wrapping.

Until then, RU coverage relies on:
- OpenAlex (which catalogs ~80% of RU work with DOIs)
- The `--lang auto` translation flow (RU query → EN parallel via
  `argos-translate`)

## eLIBRARY.ru / РИНЦ

**Status: excluded by design.** Per research §2 line 48, scraping RINC is
prohibited by their terms of service (sciguide.hse.ru, ВШЭ:
«Использовать веб-скрейпинг для сбора данных РИНЦ не рекомендуется: это
запрещено правилами и грозит блокировкой по диапазону IP-адресов»).

ultrasearch will **not** add eLIBRARY scraping in any future stage. Users who
need RINC coverage should:
1. Search eLIBRARY manually
2. Extract DOIs of relevant papers
3. Re-query ultrasearch with the DOIs to populate the corpus via OpenAlex /
   Crossref

## РГБ (Russian State Library)

The РГБ catalog (`search.rsl.ru`) is searchable but has no API. Scraping is in
a legal gray zone (terms forbid bulk download but explicit licensing for
research is undefined). ultrasearch does not include a РГБ source.

Dissertations are better served by:
- OATD (Open Access Theses & Dissertations) — future Stage 3.1 source
  (the earlier `oatd` stub was deleted from `discover.py`; not yet wired).
- BASE (Bielefeld) — likewise, future Stage 3.1 source; the `base` stub was
  removed pending a real implementation.
- The user importing PDFs locally and pointing ultrasearch at them via a
  future `--corpus-import` flag (Stage 4 idea, not planned)

## Translation flow (Stage 3 `--lang` flag)

`ultrasearch.py` defaults to `--lang auto`. The pipeline:

1. `translate.detect_lang(query)` → ISO 639-1 code (uses `langdetect`)
2. If detected language is not English:
   - Original query goes to any source that accepts UTF-8 (all of them do)
   - `translate.translate_text(query, src='ru', dst='en')` produces an EN
     parallel query
   - The EN query is sent to OpenAlex / S2 / arXiv / Crossref / Europe PMC /
     CORE / DataCite / HuggingFace
3. Dedup runs on the union — DOIs collapse across languages
4. Abstracts of indexed RU papers can be batch-translated via
   `translate.py --abstracts --query-lang ru` — populates
   `papers.abstract_translated` so synthesis sees both languages

Argos-translate downloads ~50MB per language direction on first use. Cached at
`~/.local/share/argos-translate/`.

## Recommended workflow for Russian-language research

```bash
# Set up env vars (Stage 1+2 required)
export OPENALEX_API_KEY="..."
export UNPAYWALL_EMAIL="you@example.com"

# Run with auto language detection — most RU queries return useful hits via
# OpenAlex/Crossref's RU-DOI catalogs alone.
python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py ultrasearch.py \
    "нейроинтерфейсы P300 систематический обзор" \
    --max-papers 30 \
    --lang auto \
    --out /tmp/p300-review.md

# Stage 3 full: 3-stage retrieval + multi-section synthesis (slower, higher
# quality). Translations of RU abstracts cached in corpus.db for the next run.
python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py ultrasearch.py \
    "нейроинтерфейсы P300 систематический обзор" \
    --max-papers 50 \
    --lang auto \
    --profile full \
    --render-graph \
    --out /tmp/p300-deep-review.md
```

The `--profile full` path triggers cross-encoder rerank + RCS-cached scoring
(retrieve.py 3-stage). RCS scoring itself is performed by ultrasearch.py
calling Claude as a subagent against `prompts/rcs-summary.txt` for each chunk
that survives Stage B — these scores are cached by `query_hash` in the
`rcs_scores` table for subsequent identical-query reruns.
