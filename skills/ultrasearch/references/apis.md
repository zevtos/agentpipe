# Academic API reference — ultrasearch

Endpoint specs, rate budgets, and environment variables for the discovery sources. The first three (OpenAlex, Semantic Scholar, arXiv) are documented in full; the later additions (Crossref, Europe PMC, CORE, DataCite, HuggingFace) ship too and are summarized in the tables below.

## Environment variables

| Var | Mandatory? | Used by | Purpose |
|---|---|---|---|
| `OPENALEX_API_KEY` | **Yes** after 13 Feb 2026 | `discover.py` (OpenAlex) | Free API key from <https://openalex.org/account>. Without it OpenAlex returns 401 to new users. |
| `OPENALEX_EMAIL` | Strongly recommended | `discover.py` (OpenAlex polite pool) | Identifies you in OpenAlex logs. Improves throughput in the polite pool. |
| `UNPAYWALL_EMAIL` | **Yes** | `fetch.py` Tier 2 | Required by Unpaywall TOS. Without it the Unpaywall tier is skipped — the largest OA tier after the free OpenAlex hint. |
| `S2_API_KEY` | Optional | `discover.py` (Semantic Scholar) | Dedicated 1 RPS pool. Without it you share the 5000-req-per-5-min anonymous bucket with the world. |

Set them in your shell before invoking the skill:

```bash
export OPENALEX_API_KEY="..."
export OPENALEX_EMAIL="you@example.com"
export UNPAYWALL_EMAIL="you@example.com"
export S2_API_KEY="..."           # optional
```

A one-line pre-flight check:

```bash
for v in OPENALEX_API_KEY OPENALEX_EMAIL UNPAYWALL_EMAIL S2_API_KEY; do
    printf "%-22s %s\n" "$v" "${!v:+set}"
done
```

## OpenAlex

- Base URL: `https://api.openalex.org`
- Endpoint used: `GET /works`
- Auth: `?api_key=<KEY>` query param OR `Authorization: Bearer <KEY>` header
- Polite pool: `?mailto=<EMAIL>` (Stage 1 sends both api_key and mailto)
- Hard limit: 100 RPS; our budget: **10 RPS** (`aiometer`)
- Fields requested: `id,doi,title,display_name,publication_year,authorships,primary_location,best_oa_location,cited_by_count,referenced_works,abstract_inverted_index,ids`
- Abstracts arrive as an inverted index `{word: [positions]}` and are reconstructed by `_abstract_from_inverted_index()` in `discover.py`
- `referenced_works` is the **only** Stage 1 source that ships backward citation links — kept for Stage 2 traversal (PaperQA2 Algorithm 1)
- Mandatory API-key deadline: **13 Feb 2026**

## Semantic Scholar (Graph API)

- Base URL: `https://api.semanticscholar.org/graph/v1`
- Endpoint used: `GET /paper/search`
- Auth: `x-api-key: <KEY>` header (optional)
- Shared rate limit: 5000 req / 5 min across all anonymous callers
- Dedicated rate limit with key: **1 RPS** — our budget matches.
- Fields requested: `title,abstract,year,authors,venue,citationCount,externalIds,openAccessPdf,paperId`
- `externalIds.DOI` and `externalIds.ArXiv` give us cross-source dedup keys

## arXiv

- Base URL: `http://export.arxiv.org/api/query` (via `arxiv` Python package)
- Auth: none
- Rate limit recommendation: **3 s between requests** — we set `delay_seconds=3.0` and our aiometer budget is 0.33 RPS
- Bare-ID detection: if the query looks like an arXiv ID (digits + dot + digits) we use `id_list` for direct lookup, which costs zero search-quota
- Sort: by relevance for keyword queries, default order for ID lookups

## Unpaywall (fetch Tier 2)

- Base URL: `https://api.unpaywall.org/v2`
- Endpoint used: `GET /v2/{DOI}?email=<EMAIL>`
- Auth: email query parameter (free)
- Soft cap: 100k req/day
- Returns `{is_oa, best_oa_location: {url_for_pdf, url, ...}}` — we follow `best_oa_location.url_for_pdf` first, then `best_oa_location.url`

## Shipped Stage 2/3 sources (summary)

All implemented via direct REST in `discover.py` / `quality.py` / `zotero.py` (no habanero/paperscraper — not in requirements.txt):

| Source | Endpoint | Notes |
|---|---|---|
| Crossref | `https://api.crossref.org/works` | DOI metadata + backward refs for traversal |
| Europe PMC | `https://www.ebi.ac.uk/europepmc/webservices/rest/search` | bio/medical; PDF tier reserved (XML only) |
| CORE | `https://api.core.ac.uk/v3` | 449M OA articles; `CORE_API_KEY` enables it, other sources cover without |
| DataCite | `https://api.datacite.org/dois` | datasets + grey lit (Stage 3) |
| HuggingFace Papers | hub daily-papers API | ML-domain (Stage 3) |
| Retraction Watch | CSV from gitlab.com/crossref/retraction-watch-data | `--refresh-retractions`; DOI join via `quality.py` |
| Zotero export | Zotero Web API v3 (`pyzotero`-compatible REST) | `--export-zotero`; needs `ZOTERO_API_KEY` + `ZOTERO_USER_ID` |
| sci-hub `--grey` | opt-in tier 8 | legal gray zone — explicit flag + disclaimer only |

## Still deferred (post-v1.0)

| Source | Status |
|---|---|
| КиберЛенинка (OAI-PMH), OATD, BASE | stubs registered; OAI-PMH parsing pending (v1.1) |

## Troubleshooting

**OpenAlex returns 401:** `OPENALEX_API_KEY` missing or invalid. Re-issue at <https://openalex.org/account>.

**S2 returns 429 frequently:** you're sharing the 5000-per-5-min anonymous bucket. Get a free dedicated key at <https://www.semanticscholar.org/product/api>.

**arXiv hangs:** the `arxiv` package has a built-in 3 s sleep between requests. A 50-result query takes ~25-30 s with no concurrency. This is by design — arXiv asks crawlers to respect it.

**Unpaywall returns 404:** the DOI has no OA version known to them. The cascade continues automatically (arXiv, publisher templates, Zenodo); `--grey` is the explicit last-resort opt-in.
