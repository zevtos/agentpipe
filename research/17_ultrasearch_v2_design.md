# ultrasearch v2: дизайн-документ универсального исследовательского скилла для Claude Code

**TL;DR**
- v2 ultrasearch = v1 academic pipeline + **семь профилей источников** (academic / dev / product / startup / regulatory / docs / community) с весами, маршрутизируемые **классификатором-субагентом**, плюс **рекурсивный режим** в стиле GPT-Researcher Deep Research (breadth × depth tree) для случаев типа «крипто-кошелёк со всеми сетями».
- Архитектурно: пишем `profiles/*.yaml`, добавляем `crawl4ai` (deep crawl + llms.txt) и десяток pip-only клиентов (`stackapi`, `gdeltdoc`, `defillama-sdk`, `bip_utils`, `cosmpy`, `solders`, `pytoniq`, `tronpy`, `eth-account`, `praw`, `courtlistener-api-client`, `gdelt`, SEC EDGAR через requests). Корпус `corpus.db` (sqlite-vec) сохраняется и переиспользуется — это и кэш, и rerank-индекс между профилями.
- CLI: `/ultrasearch "topic" [--profile=auto|dev|...] [--recursive] [--kb=path] [--depth=N --breadth=M]`. По умолчанию — автоопределение профиля и плоский режим; флаг `--recursive` включает дерево с фан-аутом subagents и аггрегацией в mkdocs-структуру.

---

## Key Findings

**1. Существующие deep-research системы сходятся к одной топологии.**
GPT-Researcher Deep Research, LangChain `open_deep_research`, smolagents Open Deep Research (HuggingFace), Stanford STORM/Co-STORM, LearningCircuit local-deep-research и David Zhang's `deep-research` (~500 строк, рекурсивный цикл) — все используют один и тот же шаблон: **планировщик → параллельные search/execute субагенты → reflection/critic → синтез с цитатами**. GPT-Researcher явно описывает «tree-like exploration pattern: breadth at each level, depth recursively, concurrent async/await, smart context management» (docs.gptr.dev/docs/gpt-researcher/gptr/deep_research) и заявляет конкретные числа: «Each deep research operation costs approximately $0.40 (using `o3-mini` on "high" reasoning effort)… Complete research in around 5 minutes» (docs.gptr.dev/blog/2025/02/26/deep-research). LangChain Open Deep Research добавляет «manual tool orchestration» вместо стандартного ReACT, потому что «when a tool is actually spawning an entire subgraph (like a research sub-agent), you need more control» (bolshchikov.com). STORM добавляет **multi-perspective Q&A между LLM-experts и LLM-host** и итеративный outline-refinement — это именно то, чего нет в обычных Deep Research модах LLM-вендоров, и что стоит украсть. HuggingFace open-source DeepResearch блог демонстрирует, что «using an agentic framework bumps performance by up to 60 points!» поверх frontier-модели (huggingface.co/blog/open-deep-research), но их smolagents-replica реально достигает только «55% pass@1 on the GAIA validation set, compared to 67% for the original Deep Research» (github.com/huggingface/smolagents/tree/main/examples/open_deep_research) — то есть полная реплика OpenAI Deep Research пока не завершена, ideas берём, baseline понимаем.

**2. Claude Code в апреле 2026 предоставляет нативные примитивы, ровно подходящие под рекурсивный research.**
Skills (Oct 2025), Subagents (late Jul 2025), Agent Teams (early 2026), async subagents (Dec 2025), `context: fork` во frontmatter, `Task` tool для параллельного фан-аута — всё это документировано в code.claude.com/docs/en/skills и связанных гайдах. Async subagents с `Ctrl+B` для фонового исполнения и каждый со своим контекст-окном — буквально готовая инфраструктура для recursive ultrasearch. У subagent'ов «its own system prompt, its own context window, its own tool permissions, and can run on a different model than the parent agent» (theaiarchitects.com).

**3. Бесплатная экосистема источников в 2026 расширилась и достаточна для не-академических доменов.**
Подтверждённые бесплатные/no-key или generous-free-tier API: GitHub REST/GraphQL, GitLab, **Stack Exchange API 2.3** («The daily request limit is 10,000 requests per day for applications with an API key… Without an API key, the daily limit is only 300 requests per day», api.stackexchange.com), Hacker News Algolia (без ключа), Reddit free tier (60 req/min через PRAW, нужен OAuth, **Pushshift умер, public 2023 pricing changes** убили коммерческий tier), GDELT 2.0 Doc API (`gdeltdoc`, без ключа), Wikipedia REST + Wikidata SPARQL, **SEC EDGAR EFTS** (`efts.sec.gov`, no key, 10 req/sec, всё с 2001 года), CourtListener (free tier с токеном, MCP-сервер `mcp.courtlistener.com` есть), USPTO PatentsView/ODP (бесплатно, key recommended), federalregister.gov, EUR-Lex, IETF datatracker, DefiLlama (без ключа на free), Etherscan V2 (один ключ → 60+ EVM-чейнов, 5 req/s, 100k/day — но в ноябре 2025 Avalanche/Base/BNB/OP убраны с free на Lite-tier), CoinGecko free, ProductHunt GraphQL free, deps.dev (Google), pypi.org/pypi JSON, registry.npmjs.org, crates.io, pkg.go.dev. **Crunchbase/SimilarWeb/BuiltWith** — paywalled; для startup-профиля заменяем комбинацией SEC EDGAR + GitHub-сигналы + GDELT + ProductHunt + Wayback Machine + Wikipedia.

**4. llms.txt — высокоценный сигнал для documentation-профиля, но не для всего, и НЕ потому что AI-crawlers его читают.**
По состоянию на май 2026 (presenc.ai, limy.ai, codersera.com): llms.txt принят почти всеми serious developer-doc сайтами (Anthropic, Stripe, Cursor, Cloudflare, LangChain, документация TON), Cursor/Windsurf/Claude Code/Copilot фетчат `/llms.txt` и `/llms-full.txt` рутинно. LangChain выпустил `mcpdoc` MCP-сервер для exposing llms.txt в host applications. Однако реальные AI-search crawlers практически НЕ читают этот файл: limy.ai измерили «over 500M AI bot visits across a 90-day window — only 408 targeted llms.txt directly. That's negligible for all AI crawler traffic» (limy.ai/blog/llms.txt-in-2026-the-full-guide) — то есть ~0.000008% AI-bot трафика. Вывод: **для ultrasearch llms.txt = pre-flight check на стороне нашего собственного crawler-а**: если есть, мы получаем курированный список URL «что важно» и сразу обходим только их, экономя десятки тысяч токенов crawl-а. Если нет — fallback на sitemap.xml → BFS crawl. Полагаться на llms.txt как глобально-распространённый стандарт нельзя — реализуем как opportunistic optimization.

**5. Crawl4AI — правильный выбор для documentation-deep-crawl в pure-Python 2026.**
`pip install crawl4ai` → `crawl4ai-setup`. Лицензия **Apache 2.0** (docs.crawl4ai.com), активный (v0.8.6 — 24 марта 2026, pypi.org/project/Crawl4AI/), «After powering 51K+ developers» (по их README — 51k+ разработчиков, не звёзд), Playwright под капотом, Markdown-вывод. Поддерживает: BFS/DFS/BestFirst deep crawl, `FilterChain` по домену/паттерну/SEO/relevance, `KeywordRelevanceScorer` для скоринга ссылок, `AdaptiveCrawler` (останавливается когда достаточно релевантного контента), `max_depth`/`max_pages`/`score_threshold`, prefetch=True для 5-10x ускорения discovery, crash recovery с `resume_state`. Альтернативы: Scrapy (battle-tested, но boilerplate), trafilatura (хорошо для одиночных страниц, не для сайт-level crawl), ScrapeGraphAI (требует LLM-вызов на каждую страницу — дорого). **Не Firecrawl** — paid SaaS. Для документации с тяжёлым JS (Mintlify, Docusaurus, MkDocs Material) Crawl4AI через Playwright справляется; для статики достаточно `trafilatura.spider`.

**6. Для крипто-кошелька «все сети» в Python 2026 экосистема покрывает практически всё одной мульти-чейн библиотекой плюс несколько узких.**
**`bip_utils` 2.12.1** покрывает BTC/ETH (+ все EVM с coin_type 60)/SOL/ATOM/TRX/XRP/ADA/XMR/Polkadot/NEAR/Aptos/SUI — 100+ монет, один API для mnemonic→address. **`hdwallet` 3.x** — альтернатива, 200+ криптовалют. Доп. узкие либы (для подписи транзакций, не только адресов): `eth-account` 0.13.7 + `web3` 7.16, `bitcoinlib` 0.7.8, `solders` 0.27.1 + `solana` 0.36.11 (recommended by Solana docs), `cosmpy` 0.11.2 (Apache-2.0, fetchai, alive), `pytoniq` 0.1.43 (flagship TON Python), `tronpy` 0.6.2, `substrate-interface` 1.7.11 (Polkadot — но релизы замедлились с октября 2024), `py-near`, `aptos-sdk` 0.11.0, `pysui` 0.99. Каноничные источники документации per-network известны и стабильны (см. таблицу в Details). **Cosmos chain-registry** — ключевой open data-source: `raw.githubusercontent.com/cosmos/chain-registry/master/<chain>/chain.json` даёт chain_id, bech32_prefix, slip44, fees, public RPC/REST массивы — то есть машинно-читаемая база знаний для всей Cosmos-ветки рекурсивного research.

**7. Quality-сигналы должны быть профиль-специфичны, и это критическое отличие от v1.**
v1 использует citation count / journal IF / h-index. v2 нужны разные шкалы:
- **dev**: GitHub star velocity (через star-history/OSS Insight), last commit, contributors, issues open/closed ratio, PyPI/npm download trend (`pypistats`, `pepy.tech`), Snyk Advisor health, deprecation flag, archived flag. Звёзды-каунт сам по себе бесполезен (gameable) — нужен trend.
- **community (SO/HN/Reddit)**: score, accepted flag, author rep, recency vs evergreen — `score / (age_days^1.5)` rolling.
- **docs**: official > unofficial; **наличие llms.txt = +trust сигнал** (как индикатор того, что владельцы заботятся о машинной читаемости); версия документации matches query temporal context.
- **news**: cross-source verification count (GDELT theme-grouping), originator vs aggregator, AllSides/Ad Fontes (есть public CSV; NewsGuard — платный).
- **product/startup**: founded year, funding (SEC EDGAR S-1, Form D), Wayback first-seen, ProductHunt rank, GitHub star velocity если open-source.

---

## Details

### A. Source ecosystem map (Python pip-only, free, May 2026)

| Профиль | Источник | Python | Auth | Rate limit | Уникальность |
|---|---|---|---|---|---|
| dev/code | GitHub REST | `PyGithub` 2.x / `ghapi` (fastai) | optional PAT (5000 req/h) | без PAT — 60/h | поиск кода, issues, releases, stars trend |
| dev | GitLab | `python-gitlab` | optional | разумные | репо/issues |
| dev | deps.dev (Google) | `requests` REST | none | щедрые | dependency graphs, security advisories |
| dev | OSS Insight (PingCAP) | REST/playground | none | щедрые | star-history, top trends |
| dev | grep.app | нет официального API; HTML scrape | – | – | поиск кода поверх 500k+ репо |
| pkg | PyPI | `pypi.org/pypi/{name}/json` requests | none | щедрые | metadata, releases |
| pkg | pypistats | REST | none | щедрые | download trends |
| pkg | npm | `registry.npmjs.org/{name}` + `api.npmjs.org/downloads` | none | щедрые | |
| pkg | crates.io | REST | none | 1 req/s | |
| pkg | pkg.go.dev / proxy.golang.org | REST | none | щедрые | |
| Q&A | Stack Exchange API 2.3 (170+ sites) | `StackAPI` 0.1.12 или прямо requests | без ключа 300 req/day, **с бесплатным ключом 10 000/day** (api.stackexchange.com) | per-site `/questions`, `/search/advanced`, `/answers`, `/tags`, backoff field в response | crypto.SE, ethereum.SE, bitcoin.SE, math.SE и пр. |
| Q&A | Hacker News | `hn.algolia.com/api/v2` requests | none | щедрые | search + comments |
| forum | Reddit | `praw` 7.x | OAuth client/secret (free) | 100 req/min на OAuth (PRAW защёлкивает на 60) | personal/research use ok; **commercial — paid с 2023, $0.24/1k req** |
| forum | Lobste.rs | RSS / `/s/<id>.json` | none | щедрые | dev-focused |
| forum | Bluesky AT | `atproto` Python | none для публичного | щедрые | децентрализованный соцпоток |
| forum | Mastodon | `Mastodon.py` per-instance | optional | per-instance | федеративный поиск |
| docs | llms.txt / llms-full.txt | requests (no lib needed); LangChain `mcpdoc` если хочется MCP | none | – | официально курированный список релевантных URL; **pre-flight check для любой /docs.example.com** |
| docs | sitemap.xml | `requests` + `lxml` | none | – | fallback discovery |
| docs | Crawl4AI | `crawl4ai` 0.8.6 (Apache 2.0, Playwright) | none | per-domain throttling | BFS/DFS/BestFirst deep crawl, AdaptiveCrawler, scorers, filters |
| docs | trafilatura | `trafilatura` | none | – | хороший fallback для статических страниц + spider mode |
| docs | ReadTheDocs | REST `/api/v3/` | optional token | щедрые | versioned docs, manifests |
| docs | Algolia DocSearch | per-site `appId/apiKey` встроены в HTML — извлекаются | quasi-public | щедрые | search-as-you-type индекс многих OSS-сайтов |
| news | GDELT 2.0 | `gdeltdoc` (Doc API) + `gdelt` (raw files) | none | щедрые | глобальные новости, CAMEO event coding, tone, themes |
| news | Google News RSS | `feedparser` `news.google.com/rss/search?q=` | none | щедрые | |
| news | NewsAPI.org | free-key 100 req/day | – | низко | last resort, не основной |
| encyclopedic | Wikipedia REST | `wikipedia-api` / requests | none | щедрые | |
| encyclopedic | Wikidata SPARQL | `SPARQLWrapper` → `query.wikidata.org/sparql` | none | щедрые с throttle | structured facts, entity linking |
| business | SEC EDGAR EFTS | requests на `efts.sec.gov/LATEST/search-index` | none, User-Agent обязателен | 10 req/s | full-text всех filings с 2001, S-1/10-K/10-Q/8-K, Form D |
| business | SEC EDGAR submissions API | `data.sec.gov/submissions/CIK{n}.json` | none | 10 req/s | structured данные компании |
| business | Companies House UK | REST | free API key | щедрые | UK филинги |
| business | OpenCorporates | REST free tier | optional | низко | global корпоративные данные |
| business | ProductHunt | GraphQL | OAuth token (free) | щедрые | продукты и лидеры |
| trends | pytrends (Google Trends) | `pytrends` | none | rate-limited Google → flaky, ставить ретраи + sessions | поисковые тренды; альтернатива: GDELT volume timeline |
| regulatory | federalregister.gov | REST | none | щедрые | |
| regulatory | EUR-Lex | SOAP/REST | free key | щедрые | |
| regulatory | regulations.gov | REST | free key | щедрые | |
| regulatory | CourtListener | `courtlistener-api-client` от Free Law Project + MCP сервер | free token | щедрые | US case law millions opinions |
| patents | USPTO ODP / PatentsView | `requests` POST к `api.patentsview.org` | optional | щедрые | поиск 8M+ патентов |
| patents | EPO Open Patent Services | requests | free tier key | низко | EU patents |
| patents | Google Patents Public Datasets | BigQuery (требует GCP auth) | gcloud auth | – | для очень больших запросов |
| video | yt-dlp | `yt-dlp` | none | rate-limited YouTube | загрузка аудио/auto-captions без API |
| video | youtube-transcript-api | `youtube-transcript-api` | none | – | когда captions есть |
| audio | faster-whisper (mlx-whisper на macOS arm64) | `faster-whisper` / `mlx-whisper` | local | – | транскрипция локально на Apple Silicon |
| podcast | Podcastindex.org | REST | free API key/secret | щедрые | open podcast directory |
| crypto | DefiLlama | `defillama-sdk` (official) / `DeFiLlama` (community) | none для free endpoints | щедрые | TVL, цены, stablecoins, fees |
| crypto | CoinGecko | `pycoingecko` | optional demo key | 10-50 req/min free | цены, market cap |
| crypto | Etherscan V2 | requests | free API key | **5 req/s, 100k/day**; Base/BNB/OP/AVAX вынесены на Lite tier с ноября 2025 | 60+ EVM chains один ключ |
| crypto | Cosmos chain-registry | прямой git fetch `raw.githubusercontent.com/cosmos/chain-registry/master/<chain>/chain.json` | none | github raw rate | canonical база chain_id/bech32/slip44/RPC/REST для всей экосистемы |
| crypto | chainlist.org / DefiLlama chainlist | github `DefiLlama/chainlist` raw | none | – | EVM chainId/RPC registry |
| crypto | The Graph | GraphQL public subgraphs | optional | щедрые на public | DEX/protocol indexed data |
| crypto | Snapshot | GraphQL | none | щедрые | DAO governance |

### B. Topic classification → profile routing

Решение: **классификатор-субагент с few-shot промптом**, выдающий weighted JSON (а не single label). Это совпадает с тем, как LangChain `open_deep_research` использует «Research Brief» fase для контекстного routing (langchain blog, July 2025).

Структура:
```yaml
# .claude/skills/ultrasearch/classifier_prompt.md
Дано пользовательское query. Верни JSON:
{ "profiles": {"academic": 0.0-1.0, "dev": ..., "product": ..., "startup": ..., "regulatory": ..., "docs": ..., "community": ...},
  "is_recursive": bool, "branching_keys": [...],   # для "wallet supporting all networks" → ["network"]
  "output_format": "literature_review|adr|matrix|kb|lean_canvas|skill_design|source_list",
  "clarifications_needed": [] | [{"q": "...", "why": "..."}]   # не более одного
}
```

Дешевле, чем zero-shot transformers, и точнее keyword-matching. Один LLM-вызов на старте. Если max(profile_weight) < 0.4 — задаём ровно один уточняющий вопрос. Multi-label всегда — реальные query типа «build a language learning app» = 0.5 dev + 0.3 product + 0.2 academic (learning science).

### C. Recursive/branching orchestration

Топология (вдохновлено GPT-Researcher + LangGraph + Claude Code async subagents):

```
LeadResearcher (main session)
 ├─ Classifier subagent → profile weights + branching_keys
 ├─ If is_recursive:
 │    Decomposer subagent → list[Branch] (e.g., [EVM, Solana, Cosmos, Bitcoin, TON, Tron, Polkadot, NEAR, Aptos, Sui])
 │    For each branch (PARALLEL fan-out via Task tool, async):
 │        BranchResearcher subagent (context: fork)
 │          ├─ inherits parent profile weights (rerun classifier if branch differs)
 │          ├─ executes flat ultrasearch pipeline scoped to branch
 │          └─ writes branch artifact to kb_dir/branches/{branch_slug}.md + JSON facts
 │    Aggregator subagent → cross-branch dedup (semantic via sqlite-vec), unified index, top-level overview
 ├─ Else (flat):
 │    QueryPlanner → multi-perspective queries (STORM-style)
 │    Searcher subagents (PARALLEL): one per top source bucket weighted by profile
 │    Ranker → cross-source scoring per profile metric
 │    Synthesizer (STORM iterative refinement: outline → fill → critic → polish)
 └─ Output writer → template per output_format
```

Параметры (рекомендуемые дефолты, заимствованы из GPT-Researcher и LangGraph):
- `breadth` (queries per level) = **4**, `depth` (recursion levels) = **2** для flat, до **3** для recursive
- `max_branches` = **12** (защёлка на user-facing случаях типа crypto wallet)
- `branch_timeout` = 8 минут (GPT-Researcher Deep Research приводит «approximately $0.40 (using o3-mini on "high" reasoning effort)… around 5 minutes», docs.gptr.dev/blog/2025/02/26/deep-research)
- Стопинг: branch завершается когда AdaptiveCrawler.is_satisfied() == True OR max_pages OR no_new_information_for_2_iterations (STORM-style critic)

Failure handling: одна неудача branch → retry один раз с упрощённым prompt; вторая неудача → branch помечается `status: failed`, попадает в финальный отчёт как gap. **Не abort**.

Re-merging: после всех branches Aggregator embedd-ит **по chunk-у** все branch-outputs в общий sqlite-vec и кластерит cosine > 0.85 — кросс-чейн дубликаты (например, «EIP-155 chain ID concept» появится и в EVM, и в Cosmos EVMOS branch). Дубликаты в финальном отчёте схлопываются в shared «Common Concepts» секцию.

### D. Deep documentation crawling — конкретный pipeline

```python
async def crawl_docs(root_url: str) -> list[Document]:
    # 1. Pre-flight: llms.txt
    llms = await try_fetch(f"{root_url}/llms.txt") or await try_fetch(f"{root_url}/llms-full.txt")
    if llms:
        urls = parse_llms_txt(llms)  # markdown с категорированными ссылками
        return await fetch_all(urls)  # batch
    # 2. Sitemap fallback
    sitemap = await try_fetch(f"{root_url}/sitemap.xml")
    if sitemap:
        urls = parse_sitemap(sitemap)
        return await fetch_all(urls[:500])
    # 3. Crawl4AI deep BFS
    from crawl4ai import AsyncWebCrawler, BFSDeepCrawlStrategy
    from crawl4ai.deep_crawling import FilterChain, DomainFilter, KeywordRelevanceScorer
    async with AsyncWebCrawler() as crawler:
        result = await crawler.arun(
            url=root_url,
            config=CrawlerRunConfig(
                deep_crawl_strategy=BFSDeepCrawlStrategy(
                    max_depth=3, max_pages=200,
                    filter_chain=FilterChain([DomainFilter(allowed=[urlparse(root_url).netloc])]),
                    url_scorer=KeywordRelevanceScorer(keywords=topic_keywords, weight=0.7),
                    score_threshold=0.3,
                ),
                cache_mode=CacheMode.ENABLED,
            ),
        )
        return [Document(url=r.url, markdown=r.markdown) for r in result]
```

Версионирование документации (важно для Telegram Mini Apps — там есть changelog по версиям Bot API): извлекаем `version` / `since` markers regex-ом из markdown, складываем в `corpus.db` колонку. Mintlify/Docusaurus/MkDocs Material — все нормально через Crawl4AI; ReadTheDocs дополнительно даёт `objects.inv` (Sphinx inventory) для structured навигации.

### E. Quality scoring per profile (concrete)

```python
# scoring.py
def score_dev(repo: GHRepo) -> float:
    star_velocity = stars_added_last_90d / max(stars_total, 1)
    last_commit_days = (now - repo.pushed_at).days
    recency = exp(-last_commit_days / 180)
    health = 1.0 if not repo.archived else 0.1
    issues_ratio = repo.closed_issues / max(repo.total_issues, 1)
    return 0.3*log1p(repo.stars) + 0.25*star_velocity + 0.25*recency + 0.1*health + 0.1*issues_ratio

def score_community(post: SOPost) -> float:
    age_days = (now - post.created).days
    return (post.score + (5 if post.accepted else 0)) / (age_days ** 0.5 + 1)

def score_news(article: GdeltDoc) -> float:
    # cross-source verification = сколько ещё доменов писало про тот же GDELT event/theme
    return article.cross_source_count * 0.5 + abs(article.tone) * 0.1 + recency_score
```

### F. Что украсть из существующих систем

| Проект | Что украсть | Что не брать |
|---|---|---|
| GPT-Researcher | Breadth×Depth tree exploration + async/await concurrency + Skill installation pattern (у них уже есть `.claude/skills/`) | Tavily dep (paid); их vector store (мы уже используем sqlite-vec) |
| LangChain open_deep_research | Research Brief stage + Manual tool orchestration вместо ReACT для subgraph-tools | LangGraph dependency overhead для нашего pure-Python скилла |
| smolagents open_deep_research | Code-thinking agents (если/когда захочется) | Зависимость от SerpAPI/Serper (paid) |
| STORM/Co-STORM | Multi-perspective Q&A между LLM-experts, outline iterative refinement, full citation grounding | Не нужна Bing dep — у нас mix open sources |
| local-deep-research | 10+ search engines, encryption, local-first | Docker-only архитектура; SearXNG зависимость |
| Perplexica | Routing по типу запроса | Self-hosted SearXNG |
| HuggingFace open-deep-research blog | Подтверждение, что агентный фреймворк даёт «up to 60 points» прироста поверх baseline LLM (huggingface.co/blog/open-deep-research) | Опять SerpAPI |
| David Zhang dzhng/deep-research | Минимализм — рекурсия в ~500 строк | – |
| ResearchAgent / AgentForge | Patterns для reflection/critic | Тяжёлые фреймворки |

### G. Output templates (per output_format)

- `literature_review.md.j2` — v1 (academic) — без изменений
- `adr.md.j2` — Architecture Decision Record (Michael Nygard формат): Context / Decision / Status / Consequences / Sources
- `library_matrix.md.j2` — таблица: lib × {stars, last_commit, py_compat, license, install, code_snippet, weakness}
- `kb_index.md.j2` + `branches/*.md` + `facts.yaml` — для рекурсивного режима (mkdocs-совместимый layout)
- `lean_canvas.md.j2` — startup-профиль: Problem / Customer Segments / UVP / Solution / Channels / Revenue / Cost / Key Metrics / Unfair Advantage
- `feature_matrix.md.j2` — product-профиль: competitor × feature grid
- `compliance_checklist.md.j2` — regulatory: jurisdiction → applicable rules → citations → status
- `skill_design.md.j2` — meta-профиль: SKILL.md draft + suggested subagents + tool list

### H. v2 architecture (concrete files)

```
.claude/skills/ultrasearch/
├── SKILL.md                        # v2: описание + flags + invocation rules
├── classifier.md                   # subagent prompt (frontmatter: context: fork)
├── decomposer.md                   # subagent prompt for branching
├── searcher.md                     # subagent prompt (generic, parameterised by profile)
├── ranker.md
├── synthesizer.md                  # STORM-style multi-perspective
├── aggregator.md                   # для recursive merge
├── profiles/
│   ├── academic.yaml               # v1 sources (OpenAlex, S2, arXiv, PubMed, Crossref, CORE, OpenAIRE, Europe PMC, Unpaywall)
│   ├── dev.yaml                    # GitHub, deps.dev, PyPI, npm, crates.io, Stack Exchange, HN
│   ├── product.yaml                # ProductHunt, GitHub, Wikipedia, GDELT, App Store search
│   ├── startup.yaml                # SEC EDGAR, GDELT, ProductHunt, GitHub, Companies House, OpenCorporates, Wayback
│   ├── regulatory.yaml             # CourtListener, federalregister, EUR-Lex, regulations.gov, USPTO, IETF
│   ├── docs.yaml                   # llms.txt → sitemap → Crawl4AI; Algolia DocSearch; ReadTheDocs
│   └── community.yaml              # Stack Exchange (all sites), HN Algolia, Reddit (PRAW), Lobste.rs, Bluesky, Mastodon
├── templates/                      # Jinja2 templates per output_format
├── scoring/                        # profile-specific quality functions (pure python)
├── sources/                        # один файл на интегратора: gh.py, stackex.py, gdelt.py, edgar.py, llamapool.py, etherscan.py, defillama.py, etc.
├── crawl/                          # crawl4ai_helpers.py, llms_txt.py, sitemap.py
├── recursive.py                    # branching orchestrator (fanned-out via Claude Code Task tool)
└── corpus.db                       # уже существует с v1 — РЕЮЗАЕМ; добавляем колонки profile, branch, scoring_meta
```

**Профиль (пример) `profiles/dev.yaml`:**
```yaml
name: dev
description: Tools, libraries, frameworks comparison and build planning
sources:
  - id: github_repo_search
    weight: 0.30
    config: { stars_min: 50, last_commit_max_days: 730 }
  - id: stack_exchange
    weight: 0.20
    config: { sites: [stackoverflow, serverfault, superuser, softwareengineering] }
  - id: hn_algolia
    weight: 0.10
  - id: deps_dev
    weight: 0.10
  - id: pypi_meta
    weight: 0.10
  - id: docs_crawl
    weight: 0.20
scoring: scoring/dev.py:score_dev
output_template: templates/library_matrix.md.j2
output_alt: [adr.md.j2, build_plan.md.j2]
```

**SKILL.md frontmatter (v2):**
```yaml
---
name: ultrasearch
description: |
  Universal research skill. Auto-detects profile (academic/dev/product/startup/regulatory/docs/community)
  or use --profile=X. Supports --recursive for branching research producing knowledge base.
  Pure Python, free APIs, macOS Apple Silicon.
allowed-tools: [Bash, Read, Write, WebFetch, Task]
context: inline           # main skill stays in context; subagents fork as needed
---
```

**CLI / invocation pattern в SKILL.md:**
```
/ultrasearch "<topic>"                              → auto-classify, flat
/ultrasearch "<topic>" --profile=dev                → force profile
/ultrasearch "<topic>" --recursive --kb=./mykb      → branching + mkdocs KB
/ultrasearch "<topic>" --depth=3 --breadth=5
/ultrasearch "<topic>" --profile=academic           → backward-compatible v1 mode
```

**Backward compat с v1:** `--profile=academic` мапится 1:1 в существующий v1 pipeline. `corpus.db` schema additive: новые колонки nullable, старые запросы продолжают работать.

**Новые pip deps (все pure-pip, macOS arm64 проверены):**
```
crawl4ai>=0.8.6     # + crawl4ai-setup post-install (Apache 2.0)
gdeltdoc>=1.x
StackAPI>=0.1.12    # либо прямо requests
courtlistener-api-client
defillama-sdk
bip_utils>=2.12.1
eth-account>=0.13.7
solders>=0.27.1
solana>=0.36.11
cosmpy>=0.11.2
pytoniq>=0.1.43
tronpy>=0.6.2
substrate-interface>=1.7.11
pycoingecko
praw>=7.7
yt-dlp
youtube-transcript-api
faster-whisper  # или mlx-whisper для Apple Silicon MPS
feedparser
SPARQLWrapper
wikipedia-api
trafilatura     # fallback к Crawl4AI
keybert         # для extract keywords для KeywordRelevanceScorer
```

### I. Конкретные пайплайны на 7 user-case'ов

**1. «Develop a Claude skill that solves problem C» — meta**
- Профиль: meta (= 0.6 dev + 0.3 docs + 0.1 community)
- Источники: GitHub topic search `claude-code-skill`, `claude-skills`; Anthropic docs (code.claude.com/docs/en/skills, platform.claude.com/docs/en/agents-and-tools/agent-skills); awesome-claude-skills lists; HN/Reddit r/ClaudeAI; примеры из обнаруженных репо (gpt-researcher Claude skill add-on; glebis/claude-skills; 0xfurai/claude-code-subagents)
- Output: `templates/skill_design.md.j2` → готовый SKILL.md draft + рекомендации subagent topology + tool whitelist + примеры в `examples/`

**2. «Develop a website that will be B»**
- Профиль: 0.5 dev + 0.4 product + 0.1 docs
- Источники: GitHub (по фреймворкам Next.js/SvelteKit/Astro/Remix/SolidStart репо со звездами trend), deps.dev для health, Stack Exchange tag activity, ProductHunt similar sites, Wikipedia на сравниваемые подходы, docs crawl основных фреймворков через llms.txt, GDELT для упоминаний tooling-трендов
- Output: `library_matrix.md.j2` (framework comparison) + `adr.md.j2` (рекомендация стека) + `build_plan.md.j2`

**3. «Make an app (with custom functionality) for learning a language»**
- Профиль: 0.4 product + 0.3 academic (learning science) + 0.2 dev + 0.1 community
- Источники: App Store/Google Play search via iTunes/RSS API, OpenAlex/PubMed на "spaced repetition", "SRS", "language acquisition" (v1 academic pipeline), Anki/Memrise/Duolingo GitHub если open repos, Reddit r/languagelearning, ProductHunt category
- Output: `feature_matrix.md.j2` (Duolingo / Babbel / Pimsleur / Memrise / Anki feature grid) + `literature_review.md.j2` (learning science) + appendix dev-stack рекомендации

**4. «Investigate how Telegram mini apps work»**
- Профиль: 0.7 docs + 0.2 dev + 0.1 community
- Источники: Crawl4AI deep crawl `core.telegram.org/bots/webapps` (нет llms.txt у Telegram core, но есть sitemap; около 250 страниц) + `core.telegram.org/api/bots/webapps` + `docs.telegram-mini-apps.com` (community docs, есть llms.txt) + GitHub topic `telegram-mini-app` (templates Telegram-Mini-Apps/reactjs-template, nextjs-template) + Stack Exchange `telegram-bot` tag + код примеров. **Извлекаем changelog versions** — у Telegram Bot API чёткая версионность methods (например `requestFullscreen` since Bot API 8.0; `addToHomeScreen`, `checkHomeScreenStatus`, `setEmojiStatus` — последующие обновления).
- Output: `kb_index.md.j2` с разделами: Architecture / initData validation / SDK comparison / Examples / Versioning / Security / Monetization (Telegram Stars)

**5. «Build a knowledge base for crypto wallet supporting ALL networks» (РЕКУРСИВНЫЙ)**
- Профиль: 0.5 docs + 0.3 dev + 0.2 academic (для криптографических основ типа BIP-32/39/44, SLIP-44)
- Decomposer branches: `[bitcoin, evm (eth+l2s), solana, cosmos_ecosystem, ton, tron, polkadot, near, aptos, sui, xrpl, cardano, stellar, monero]`
- Каждый branch (parallel via Task tool fan-out):
  - Источники: каноничные docs (Bitcoin: developer.bitcoin.org + BIPs github.com/bitcoin/bips; Ethereum: ethereum.org/en/developers/docs + eips.ethereum.org; Solana: solana.com/docs + solana.com/docs/clients/community/python; Cosmos SDK: docs.cosmos.network + cosmos/chain-registry; TON: docs.ton.org/ — у них есть docs.ton.org/llms.txt; TRON: developers.tron.network; Polkadot: docs.polkadot.com; NEAR: docs.near.org; Aptos: aptos.dev; Sui: docs.sui.io), GitHub репо официальных SDK, Stack Exchange (ethereum.SE, bitcoin.SE, etc), Cosmos chain-registry JSON для всей Cosmos-ветки
  - Python deps указаны: bip_utils как baseline + специфичная либа (eth-account 0.13.7 / bitcoinlib 0.7.8 / solders 0.27.1 / cosmpy 0.11.2 / pytoniq 0.1.43 / tronpy 0.6.2 / substrate-interface 1.7.11 / py-near / aptos-sdk 0.11.0 / pysui 0.99)
  - Извлекаем: derivation path (SLIP-44 coin_type, BIP-44 default), signing curve (secp256k1 / ed25519), address format (bech32 / base58 / hex), public RPC endpoints, fee structure, tx serialization
  - Branch artifact: `kb/branches/{network}.md` + `kb/facts/{network}.yaml`
- Aggregator → `kb/index.md` (common concepts: BIP-39 mnemonic, HD derivation, signing) + `kb/comparison.md` (network × {curve, address_format, slip44, default_path, py_lib})
- Output: mkdocs.yml + готовый сайт `mkdocs serve`

Пример `kb/facts/cosmos_hub.yaml`:
```yaml
network: cosmos_hub
chain_id: cosmoshub-4
bech32_prefix: cosmos
slip44: 118
curve: secp256k1
default_path: m/44'/118'/0'/0/0
python_lib: cosmpy>=0.11.2
public_rpc: [https://rpc.cosmos.network, ...]   # из chain-registry/cosmoshub/chain.json
docs: https://docs.cosmos.network/
```

**6. «Research a startup idea»**
- Профиль: 0.45 startup + 0.25 product + 0.15 academic + 0.15 community
- Источники: SEC EDGAR EFTS full-text (поиск конкурентов по тематическим ключам в S-1/10-K), Form D (ранние раунды), GDELT по теме (timeline-tone для market sentiment), ProductHunt (existing products), GitHub trends для tech, Reddit/HN customer signals, Companies House UK для UK-маркета, Wayback Machine first-seen дат конкурентов
- Output: `lean_canvas.md.j2` + `competitor_map.md.j2` (table: name / founded / funding / model / traction signals) + GDELT-based market sentiment chart

**7. «Collect sources to continue working on a specific idea» — general curation**
- Профиль: автоопределение, обычно mixed; output_format = `source_list`
- Источники: top-3 source category по weight × top-N результатов с recency-bias
- Output: `source_list.md.j2` — аннотированный список с per-источник quality score и однострочной аннотацией

---

## Recommendations

**Stage 1 (week 1) — фундамент v2 без рекурсии:**
1. Создать `profiles/` директорию и YAML-схему профиля; реализовать `dev.yaml` и `docs.yaml` (самые востребованные после academic).
2. Добавить `classifier.md` subagent с few-shot промптом и JSON-выходом. Запускать через `context: fork` чтобы не загрязнять main context.
3. Расширить `corpus.db` схему: колонки `profile`, `branch_id`, `quality_score`, `source_type`. Все nullable — backward-compat с v1.
4. Интегрировать `crawl4ai` + llms.txt pre-flight в `docs.yaml` pipeline. Замерить: на типичном developer-docs сайте с llms.txt должно быть в 10-50 раз меньше fetched pages.
5. Добавить два output template: `library_matrix.md.j2` и `adr.md.j2`.
   - **Бенчмарк перед переходом к Stage 2**: запустить `/ultrasearch "best Python web framework 2026" --profile=dev` и сравнить с ChatGPT/Claude.ai Deep Research — если матрица обоснованнее и source-grounded — Stage 1 done.

**Stage 2 (week 2) — рекурсия и оставшиеся профили:**
1. Реализовать `decomposer.md` и `aggregator.md` subagents.
2. `recursive.py`: orchestrator который fan-ит через Claude Code Task tool. Параметризовать `breadth/depth/max_branches`.
3. Добавить `startup.yaml` (SEC EDGAR + GDELT + ProductHunt + GitHub) и `community.yaml` (Stack Exchange + HN + Reddit).
4. Реализовать `kb_index.md.j2` + mkdocs скелет.
   - **Бенчмарк**: запустить crypto-wallet кейс с `--recursive`. Должен сгенерироваться mkdocs сайт с не менее 10 branches, не менее 80% network coverage, и каждый branch должен содержать рабочий Python-снаппет mnemonic→address.

**Stage 3 (week 3+) — refinement:**
1. STORM-style iterative refinement в `synthesizer.md` (multi-perspective Q&A с критиком).
2. Cross-branch dedup в Aggregator (sqlite-vec cosine > 0.85).
3. `regulatory.yaml`, `product.yaml`, `meta` оставшиеся профили.
4. Caching policy: shared cache между запусками на одну и ту же домен-тему (по hash query + profile).

**Triggers/thresholds для пересмотра дизайна:**
- Если Etherscan ужесточит free tier до <2 req/s → нужен `web3.py` direct-to-RPC fallback через chainlist.org public endpoints. *Watch*: ноябрьские изменения 2025 (вынос Base/BNB/OP/AVAX на Lite) — это negative signal, могут продолжить.
- Если Reddit окончательно закроет research free tier → переключаемся на read-only json эндпойнты (`<url>.json`) или Bluesky/Mastodon как community-source. *Watch*: их 2023 pricing уже сжимает free; PRAW сейчас 60 req/min.
- Если adoption llms.txt у developer-docs сайтов поднимется значительно выше текущего уровня и AI-краулеры начнут его читать — переключить порядок Crawl4AI fallback на llms.txt-first (сейчас limy.ai даёт «only 408» обращений из 515M событий — то есть AI-crawlers пока его игнорируют, но нашему собственному пайплайну это не мешает использовать его как hint).
- Если subagent fan-out в Claude Code станет soft-limit'нут (например, parallel >10 будет throttled) → разбивать branches на батчи по 5 и серийно объединять.

**Что использовать НЕМЕДЛЕННО уже сегодня, без дальнейшего research:**
- `bip_utils` 2.12.1 для всей крипто-ветки. Не пытайтесь писать derivation paths вручную.
- `crawl4ai` 0.8.6 (Apache 2.0, не Scrapy и не Firecrawl) для docs deep crawl.
- `gdeltdoc` для новостей (НЕ NewsAPI free tier — слишком зажат).
- SEC EDGAR EFTS прямо через requests (никакой `sec-api-python` платный SDK не нужен).
- Stack Exchange API напрямую через requests с бесплатным app key (StackAPI lib не критична — обёртка тонкая); free key даёт 10 000 req/day vs 300 без.

---

## Caveats

- **Recursive режим дорог по токенам.** GPT-Researcher Deep Research официально приводит «approximately $0.40 (using o3-mini on "high" reasoning effort)… around 5 minutes» (docs.gptr.dev/blog/2025/02/26/deep-research) — для Claude Sonnet с 12 branches это будет существенно. Ставьте hard cap по токенам в SKILL.md.
- **llms.txt — не магия, и AI-crawlers его в реальности не читают.** limy.ai (May 2026) измерили: «over 500M AI bot visits across a 90-day window — only 408 targeted llms.txt directly. That's negligible for all AI crawler traffic.» Мы используем llms.txt как наш собственный pre-flight optimization (10-50x экономия crawl), а не как глобально-надёжный сигнал.
- **Reddit API status шаткий.** Free tier формально жив (100 req/min на OAuth, PRAW защёлкивает на 60), но коммерческие use-cases — paid с 2023 ($0.24 за 1k req). Pushshift — мёртв.
- **Etherscan free tier сужается.** В ноябре 2025 Base/BNB/OP/Avalanche убраны с free. Это паттерн — другие explorer-сети могут последовать. Для критичных EVM-операций иметь fallback через chainlist.org public RPCs + `web3.py`.
- **GDELT не даёт full article text** — только метаданные, URL, tone, themes. Для полного текста — fetch URL отдельно + trafilatura.
- **pytrends (Google Trends) нестабилен** — Google rate-limit-ит агрессивно. Не делать его load-bearing; использовать как complement к GDELT volume timeline.
- **STORM/Co-STORM используют Bing Search по умолчанию** — мы не берём их код напрямую, а заимствуем pattern (multi-perspective Q&A между LLM-experts + iterative outline refinement).
- **substrate-interface** замедлился в релизах (1.7.11 — Oct 2024), но всё ещё канонический Polkadot Python lib. Если Polkadot будет частым branch — мониторить альтернативы (JAMdotTech/py-polkadot-sdk fork).
- **Cosmos chain-registry GitHub-based** — если GitHub raw упадёт или rate-limit-ит, нужен мирор. Решение: git clone repo раз в день в `.cache/chain-registry`.
- **bip_utils Mar 2026 release date** — PyPI отображал 2.12.1 в моменте исследования; уточняйте через `pip index versions bip_utils` перед pin'ом.
- **embit** помечен Snyk как «Inactive — no new release in 12 months» (Dec 2025) — не использовать для нового кода в Bitcoin-ветке. Брать `bitcoinlib` или `python-bitcoinlib` (последний — низкоуровневый).
- **HuggingFace open-deep-research smolagents-based** «achieves 55% pass@1 on the GAIA validation set, compared to 67% for the original Deep Research» (github.com/huggingface/smolagents/tree/main/examples/open_deep_research) — не закрывает gap до OpenAI Deep Research. То есть «open replication» пока неполная; идеи берём, но baseline понимаем. Этот же blog даёт цифру эффекта от агентного фреймворка вообще: «using an agentic framework bumps performance by up to 60 points!» (huggingface.co/blog/open-deep-research).
- **Output для рекурсивного режима — mkdocs**: пользователю нужен установленный `mkdocs` + `mkdocs-material` для финальной сборки. Если не хочет — оставить просто markdown tree.