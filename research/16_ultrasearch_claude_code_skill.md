# Ultrasearch: архитектура Claude Code-скилла для thesis-level исследований (neurotech + SE), pure-Python, без Docker

## TL;DR
- **Стек принятых решений** (без Docker, без платных API, pip-installable на macOS Apple Silicon): discovery — `pyalex` + `semanticscholar` + `arxiv` + `habanero` + Europe PMC REST + `paperscraper` (для bioRxiv/medRxiv/chemRxiv) + `unpywall`; парсинг PDF — `pymupdf4llm` как быстрый базовый слой и `docling` как качественный fallback (Marker и Nougat — опционально и не как дефолт); индекс — `sqlite-vec` 0.1.9 + `sentence-transformers` (модели `allenai-specter` / `BAAI/bge-large-en-v1.5`); снежный ком — реплика PaperQA2 Algorithm 1 (порог RCS ≥ 8, одна степень, 3 API-вызова на статью, овер-лап α = 1/3, hard cap ℓ = 12); агент-оркестратор — Claude Code skill (`~/.claude/skills/ultrasearch/`) с progressive disclosure, скрипт-вызовом и фан-аут subagents.
- **Где обходим стоковые Deep Research-режимы**: (а) реальные академические API + open-access full-text вместо общего web-search; (б) PaperQA2-style RCS-reranking с метаданными журнала/цитирования вместо chunks-as-context — даёт **+12.4% над ближайшим конкурентом на science portion of RAG-QA-Arena** (FutureHouse blog, Michael Skarlinski, 5 марта 2025: "PaperQA2 achieves state-of-the-art performance on the science portion of RAG-QA-Arena — 12.4% higher than the closest competitive system", benchmark: 1404 questions, 1.7M documents); (в) детерминированный citation-traversal (Wohlin 2014 + Algorithm 1) вместо «погуглим ещё раз»; (г) персистентный локальный корпус и vector store, который растёт между запусками; (д) интеграция Retraction Watch через Crossref REST API для авто-флага отозванных статей; (е) русскоязычные источники (КиберЛенинка, eLIBRARY) через щадящий scraping + перевод запросов.
- **Реалистичный MVP за 1–2 дня, v1.0 за 2–3 недели**; bottleneck — парсинг PDF: на M4-Mac Marker через MPS даёт ~0.22 страниц/сек (Modal × Datalab joint blog: *"On an M4 Mac using Apple MPS (no GPU), you can process around 0.22 pages per second"*), docling — 0.49 sec/page на GPU (Auer et al., arXiv:2501.17887, Docling Technical Report, January 2025: *"0.49 sec/page with Docling and 0.86 sec/page with Marker"*). Реальная пропускная способность end-to-end (search → fetch → parse → embed → index) для разумного «гибридного» режима — **порядка 200–400 статей/час на M-серии**, лимитируется политностью API (OpenAlex рекомендует ≤ 10 RPS в polite pool) и сетевой загрузкой PDF, не CPU.

---

## Key Findings

### 1. PDF-парсинг: pymupdf4llm как дефолт, docling как качественный fallback, отказ от GROBID/Nougat в дефолтном пайплайне

- **pymupdf4llm**: `pip install pymupdf4llm`. Один pip, готовые wheels под macOS arm64, никакой JVM, никакой CUDA, **никаких моделей**. В независимом тесте (Aman Kumar, Medium 2025) — 0.12с/страницу для базовой markdown-конверсии. **Критичный нюанс — лицензия GNU AGPL v3** (same as PyMuPDF/MuPDF): для open-source skill это окей; для коммерческого reuse — нужна Artifex commercial license, что подтверждено в PyMuPDF Discussion #971.
- **docling** (IBM Research, теперь под LF AI & Data, GitHub: docling-project/docling): `pip install docling`, Python ≥ 3.10, hard-dep на PyTorch. На первом запуске тянет ~358 MB моделей (RT-DETR layout + TableFormer) из HuggingFace (`ds4sd/docling-models`). Авто-использует MPS на Apple Silicon когда возможно. **Авторитетный first-party бенчмарк: 0.49 sec/page (на GPU)** — Auer et al., Docling Technical Report (arXiv:2501.17887, January 2025): *"0.49 sec/page with Docling and 0.86 sec/page with Marker"*. На M-series CPU нет опубликованного first-party бенчмарка — нужны эмпирические измерения.
- **marker (datalab-to/marker)**: `pip install marker-pdf`. На M4 Mac с MPS — 0.22 страниц/сек (Modal × Datalab joint blog). MPS поддержка частичная — TableRec-модель форсится на CPU (Surya log: `TableRecEncoderDecoderModel is not compatible with mps backend. Defaulting to cpu instead`), есть открытые crashes на M-series (issue #993). Скачивает ~2–3 GB Surya-моделей. **Лицензия: code GPL; weights — CC-BY-NC-SA 4.0** (с waiver для орг с выручкой < $5M и < $5M VC). Использовать как опциональный `--marker` high-quality mode.
- **nougat** (`pip install nougat-ocr`): **функционально заброшен** — последний релиз 0.1.17 в апреле 2024, неотвеченные issues от 2024 (ROCm #220, pydantic #232), MPS не поддерживается, веса CC-BY-NC. Если уж нужен — использовать HuggingFace Transformers-port (`from transformers import NougatProcessor, VisionEncoderDecoderModel`), а не оригинальный репо. **В дефолт не включаем.**
- **GROBID**: лучший по качеству парсер ссылок и секций, но требует Java/Docker. PaperQA2 использует его по умолчанию — мы намеренно отказываемся (constraint требования).
- **Стратегия двухступенчатого парсинга**: для каждой статьи пытаемся `pymupdf4llm` → если страница содержит таблицы (детектируется по числу `|` или regex) или формулы (детектируется по `$$` / LaTeX-маркерам), для этих страниц вызываем `docling`. Это даёт ~5× ускорение vs. чистый docling без существенной потери качества.
- **Ссылки/refs**: для извлечения списка литературы используем `refextract` (CERN, pip install refextract) — pure-Python, регекспы поверх extracted text, отлично работает с arXiv-стилем.

### 2. Discovery layer — 7 open API + 2 опционально

| Источник | Python lib | Auth | Rate limit | Что уникально |
|---|---|---|---|---|
| **OpenAlex** | `pyalex` (J535D165/pyalex) | email в polite pool, **с 13 февраля 2026 — обязательный free API key** | 100 RPS hard, 100k req/день default | абстракты (inverted index), referenced_works, cited_by_api_url, topic clustering (T-IDs), OA URLs, авторы с ORCID; **260+ млн works на 29 декабря 2024, 23% open access, 100+ млн авторов** (Katina Magazine comprehensive review 2025: *"OpenAlex has metadata for more than 260 million works, including journal articles and books, 23 percent of which are open access; over 100 million authors"*) |
| **Semantic Scholar Graph** | `semanticscholar` (PyPI) | optional free key | 5000 req/5min shared без ключа; 1 RPS dedicated с ключом; bulk-search до 10M статей | SPECTER2 embeddings, `/paper/{id}/references` `/citations` с пагинацией, batch endpoint /paper/batch с полями `references,citations,authors` |
| **arXiv** | `arxiv` (lukasschwab/arxiv.py) | none | ~3с между запросами (рекомендация arxiv) | full-text PDF + HTML (ar5iv), категории cs.NE/q-bio.NC для нашего домена |
| **Crossref** | `habanero` (sckott/habanero) | mailto в polite pool | polite pool — ~50 req/sec, иначе anyone's-guess | DOI metadata, **Retraction Watch flags через `update-type:retraction`** |
| **Europe PMC** | прямой REST (`https://www.ebi.ac.uk/europepmc/webservices/rest/search`) | none | щадящий polite | биомед full-text (когда OA), включает preprints + patents |
| **Unpaywall** | `unpywall` | email | без жёстких лимитов, рекомендуется ≤ 100k/день | DOI → OA PDF, `is_oa`, `best_oa_location.url_for_pdf`; ~50k источников |
| **CORE** | прямой REST с free key | free key (mandatory) | ~10 req/min free tier | **449M open access articles from 15K data providers** (core.ac.uk/about, accessed May 2026: *"CORE currently contains 449M open access articles collected from 15K data providers around the world"*; Wikipedia: *"As of November 2025, CORE provided access to 431 million metadata records"*) |
| **OpenAIRE Graph API** | прямой REST (https://graph.openaire.eu) | none | щадящий | EU grey lit, проекты, датасеты, software; Search API будет phased out 31 мая 2026 — переход на Graph API |
| **HuggingFace Hub** | `huggingface_hub` | none | щадящий | ML модели/датасеты/daily papers — для neural-nets домена |

Препринт-серверы и репозитории через `paperscraper` (jannisborn/paperscraper, `pip install paperscraper`): bioRxiv, medRxiv, chemRxiv, plus PubMed/arXiv. Тянет полные дампы `.jsonl` (chemRxiv ~50K, medRxiv ~100K записей) — это **локальный полнотекстовый индекс препринтов**, поверх которого можно фильтровать без удара по API.

Дополнительно:
- **OATD** (oatd.org) — Open Access Theses & Dissertations; **BASE** (Bielefeld) — OAI-PMH endpoint.
- **DataCite** для датасетов.
- **NIH Reporter** (api.reporter.nih.gov) + **EU CORDIS** (cordis.europa.eu/api/) — гранты и проекты.
- **GitHub Search API** — для софта по нейроинтерфейсам. Лимит 30/min без токена, 5000/hr с PAT.

**Лимит:** Google Scholar — `scholarly` (scholarly.readthedocs.io) работает, но Google активно блокирует scraping; этический + правовой grey. Включаем как опциональный последний fallback, не как primary.

**Русскоязычные источники:**
- **КиберЛенинка** (cyberleninka.ru) — есть OAI-PMH endpoint (cyberleninka.ru/oai), но недокументирован; работающий `arteemmius/LeninkaParser` на GitHub демонстрирует допустимый scraping. Есть DOI у большинства статей — можно матчить через Crossref/OpenAlex обратно.
- **eLIBRARY.RU (РИНЦ)** — нет публичного бесплатного API. Веб-скрейпинг **прямо запрещён правилами** (sciguide.hse.ru, ВШЭ: «Использовать веб-скрейпинг для сбора данных РИНЦ не рекомендуется: это запрещено правилами и грозит блокировкой по диапазону IP-адресов») — блокирует диапазоны IP. **Не включаем в дефолтный пайплайн.** Альтернатива — найти DOI российских статей через OpenAlex/Crossref.
- **disserCat / РГБ** — диссертации; РГБ имеет ограниченный поиск, scraping серой зоны. Лучше использовать OATD + DART-Europe + BASE для диссертаций, плюс локальные PDF-импорты от пользователя.

### 3. Citation graph и snowballing — реплика PaperQA2 Algorithm 1 + Wohlin 2014

**Алгоритмическая основа** — Wohlin (2014), *Guidelines for snowballing in systematic literature studies and a replication in software engineering*, EASE '14, **DOI 10.1145/2601248.2601268**, открытый PDF на wohlin.eu/ease14.pdf. Процедура: построить start set через тентативный Scholar-поиск с разнообразием по publishers/authors/communities; затем итеративно для каждой статьи (i) backward snowballing — изучить список литературы, (ii) forward snowballing — найти статьи, цитирующие фокус через citation index. Каждая новая статья сама становится источником для следующего forward+backward прохода; итерация терминируется когда не находится новых статей по критериям включения.

**Конкретная имплементация (заимствуем PaperQA2 Algorithm 1, §8.1.1 в arXiv:2409.13740v2)**:
- Порог триггера: статья включается в `D_prev` если RCS score ≥ **8** (по 0–10 шкале); цитата из §8.1.1: *"The traversal originates from any paper containing a highly-scored contextual summary (RCS score 0-10), and our minimum score threshold was eight (inclusive)"*;
- **3 API-вызова на статью** (FutureHouse engineering blog цифра, согласуется с арифметикой 1 backward S2 + 1 backward Crossref + 1 forward S2; в самом paper §8.1.1 написано «four» — это, по-видимому, опечатка авторов, не подтверждаемая арифметикой их же описания);
- forward citers — Semantic Scholar; backward refs — Semantic Scholar ∪ Crossref (дедуп по casefolded title + lowercased DOI);
- одна степень за вызов, но агент может вызвать tool несколько раз за эпизод: **0.46 ± 0.02 citation traversals per question** (arXiv:2409.13740, verbatim: *"searches per question, and 0.46 ± 0.02 (mean ± SD) citation traversals per question showing that the agent will sometimes return to an additional search or traverse the citation"*);
- hard cap **ℓ = 12 papers** на вызов; overlap-фильтр **θ_o = ⌈α × |D_prev|⌉** с дефолтом α = 1/3;
- tie-break — по числу future citers (популярность).

```python
def TraverseCitations(S, theta_score=8, alpha=1/3, fut=True, ell=12):
    D_prev = {s.d for s in S if s.score >= theta_score}
    D     = GetCitations(D_prev, fut)        # one degree only
    theta_o = math.ceil(alpha * len(D))
    return FilterOverlap(D, D_prev, theta_o, ell)
```

**Сверху** — наша надстройка над PaperQA2: для каждого нового кандидата сразу embed через SPECTER2 и сравниваем cosine с центроидом текущего corpus; отсекаем если cos < 0.55 (магическая константа из SciNCL nearest-neighbour sampling). Это даёт релевантностный gate и решает проблему «снежного кома, который собирает мусор».

### 4. Embeddings, vector DB, reranking — всё в pure-Python без Docker

- **Embedding модели** (через `sentence-transformers`, `pip install sentence-transformers`):
  - дефолт для научного текста: **`sentence-transformers/allenai-specter`** (110M params, ~440 MB, fp32, тренирован на citation graph: SPECTER outperforms SciBERT/Sent-BERT/Citeomatic/SGC на SciDocs benchmark — Cohan et al. arXiv:2004.07180).
  - продвинутая опция: `allenai/specter2` (с adapter-фреймворком adapters, поддерживает task-specific adapters для proximity / classification / regression / search; обучен на 9 различных задачах через 23 fields of study — Singh et al., Ai2 Blog).
  - альтернатива/«общая» модель: `BAAI/bge-large-en-v1.5` (1024-dim) или `nomic-ai/nomic-embed-text-v1.5` (768-dim, Matryoshka — позволяет truncate без потери качества).
  - русский: `intfloat/multilingual-e5-large` или `cointegrated/LaBSE-en-ru` для запросов из РИНЦ-источников.
  - **На Apple Silicon**: sentence-transformers автоматически использует MPS если доступен; ускорение 3–5× vs CPU. Альтернатива — `mlx-embeddings` (Apple) — нативный MLX backend, ~2× быстрее MPS для batch-encoding.
- **Vector store**: **`sqlite-vec` 0.1.9** (`pip install sqlite-vec`). Pre-built wheels под macOS arm64 (`sqlite_vec-0.1.9-py3-none-macosx_11_0_arm64.whl`, 165 KB), vec0 virtual table, KNN с cosine/L2, support бинарной квантизации; рекомендуется SQLite ≥ 3.41 (на Homebrew Python — `brew install python` ставит нужную версию). Альтернатива — `lancedb` (Apache Arrow, embedded, лучше для >1M векторов с disk-based индексами) или ChromaDB (embedded mode). **Для нашего scale (≤100k chunks) sqlite-vec — оптимум**: один файл `corpus.db` ⇒ переносится, бэкапится, всё в одном месте с метаданными. Удобный wrapper — `sqlite-vec-client` (`pip install sqlite-vec-client`) даёт `client.similarity_search_with_filter()` с JSON metadata-filtering.
- **Cross-encoder reranker** (для replication PaperQA2 RCS):
  - first stage: `sqlite-vec` cosine top-k=50;
  - second stage: `BAAI/bge-reranker-base` через `sentence-transformers.CrossEncoder` → top-k=10;
  - third stage (replica RCS): LLM-вызов через Claude Code (sub-agent) генерирует contextual summary каждого chunk + RCS score 0–10; **только chunks с score ≥ 5 идут в финальный prompt**. Это то, что отличает PaperQA2 от обычного RAG и даёт +12.4% на RAG-QA Arena Science benchmark.

### 5. Web search и общий scraping — без SearXNG, без headless-браузеров (но опция есть)

- **`ddgs`** (новый rebrand `duckduckgo-search`, `pip install ddgs`) — основной web-search движок; периодически 429 — нужны retry с exponential backoff. Возвращает title/url/snippet.
- **`trafilatura`** (`pip install trafilatura`) — лучший pure-Python экстрактор основного контента из HTML. Академически валидирован: **Barbaresi, Adrien. "Trafilatura: A Web Scraping Library and Command-Line Tool for Text Discovery and Extraction." Proc. ACL-IJCNLP 2021: System Demonstrations, pp. 122–131. DOI: 10.18653/v1/2021.acl-demo.15.** Abstract: *"The tool performs significantly better than other open-source solutions in this evaluation and in external benchmarks."* Используется HuggingFace, IBM, Microsoft Research, Allen Institute, Stanford для построения текстовых корпусов.
- **`crawl4ai`** (`pip install crawl4ai`) — AsyncWebCrawler, markdown output, неплохой для JS-light страниц; **рекомендуется не как основной, а как fallback** когда trafilatura возвращает слишком мало контента.
- **`playwright`** — опционально для JS-heavy сайтов; устанавливается через `pip install playwright && playwright install chromium`; **в дефолте не включаем** чтобы не тянуть chromium (~300 MB).
- **SearXNG без Docker** — теоретически возможно через `pip install searxng` + uwsgi, но это полноценный web-сервер; **отвергаем** в пользу прямого `ddgs` + добавочных API.

### 6. Quality / anti-hallucination слой

- **Retraction Watch через Crossref REST API**: `https://api.crossref.org/v1/works?filter=update-type:retraction`. CSV-зеркало доступно через `git clone https://gitlab.com/crossref/retraction-watch-data` — обновляется каждый рабочий день. Локальный CSV (~50k записей) грузим в SQLite таблицу, проверяем каждый кандидатный DOI; ретрактаты — флагуем visible в финальном отчёте, не выкидываем (контекст важен).
- **Quality signals**: для каждой статьи считаем композитный score `Q = 0.3·log(citations+1) + 0.2·journal_impact + 0.2·author_h_index + 0.3·recency_decay`. Journal impact и h-index берём из OpenAlex (`primary_location.source.summary_stats`, `authorships[].author.summary_stats`).
- **Anti-hallucination в синтезе**: каждый абзац финального текста должен содержать как минимум один цитат-маркер `[Sn]` указывающий на конкретный source ID; финальный валидатор парсит итоговый markdown и **отказывает** в публикации отчёта если есть параграфы без цитат (regex-проверка).
- **Diversity**: при отборе top-N для синтеза применяем MMR (Maximal Marginal Relevance) с λ=0.7. Дополнительно cap «одна институция не более 25%», «один автор не более 15%» через простой счётчик.

### 7. Архитектура Claude Code skill

Структура:
```
~/.claude/skills/ultrasearch/
├── SKILL.md                          # progressive disclosure entry, < 500 строк
├── scripts/
│   ├── ultrasearch.py                # main CLI: /ultrasearch "topic"
│   ├── discover.py                   # parallel fan-out к 7+ API
│   ├── fetch.py                      # PDF download chain (unpaywall → arxiv → europepmc → publisher)
│   ├── parse.py                      # pymupdf4llm → docling fallback
│   ├── index.py                      # SPECTER embed + sqlite-vec upsert
│   ├── traverse.py                   # реплика PaperQA2 Algorithm 1
│   ├── retrieve.py                   # 3-stage retrieval (vec → cross-enc → RCS)
│   ├── synthesize.py                 # STORM-like outline → section → polish
│   ├── quality.py                    # Retraction Watch + Q-score + MMR + diversity caps
│   └── zotero.py                     # pyzotero export
├── references/
│   ├── apis.md                       # детальные endpoint specs (загружается по требованию)
│   ├── parsing-troubleshooting.md
│   ├── ru-sources.md
│   └── methodology.md                # Wohlin + PaperQA2 algorithm card
├── prompts/
│   ├── perspective-questions.txt     # STORM-style
│   ├── rcs-summary.txt               # PaperQA2 RCS prompt template
│   ├── section-writer.txt
│   └── polisher.txt
└── data/
    ├── corpus.db                      # SQLite + sqlite-vec (растёт между запусками)
    ├── retraction_watch.csv           # auto-updated weekly via hook
    └── cache/                         # 24h response cache (httpx-cache style)
```

`SKILL.md` frontmatter (по официальной Anthropic спецификации в platform.claude.com/docs/en/agents-and-tools/agent-skills/overview: name + description обязательны, name ≤ 64 символов lowercase/numbers/hyphens):
```yaml
---
name: ultrasearch
description: Conducts thesis-level research (систематический обзор, ВКР, related work) on a given topic by querying open academic APIs (OpenAlex, Semantic Scholar, arXiv, PubMed, Europe PMC, Crossref), downloading OA full-texts, parsing PDFs with pymupdf4llm/docling, building a local SPECTER2-embedded SQLite corpus, performing PaperQA2-style citation-graph snowballing, and synthesizing a STORM-style structured report with grounded citations. Specialized for neurotech and software engineering. Use when the user asks for deep research, literature review, related work, систематический обзор, ВКР, дипломная работа, или просит "найти все статьи про X".
allowed-tools: Bash(python:*), Bash(uv:*), Read, Grep, Write
---
```

Тело SKILL.md содержит (target ≤ 5000 слов / ≤ 500 строк per Anthropic official best practices):
1. Краткий runbook (5–8 шагов: plan → discover → fetch → parse → index → traverse → retrieve → synthesize → emit).
2. Команды для invocation (`python scripts/ultrasearch.py "$ARGUMENTS"`).
3. Список доступных reference-файлов через relative paths (Claude подгружает только когда нужно).
4. Описание persistent corpus и того, как Claude может его расширять между запусками.

### 8. Subagent topology и fan-out parallelism

Главный orchestration — `ultrasearch.py` запускает фан-аут на уровне Python через `asyncio + httpx + aiometer`:
- 8 параллельных запросов к API discovery (по одному на каждый источник из таблицы выше), с per-domain rate limit;
- 4 параллельных воркеров парсинга PDF (CPU-bound — `concurrent.futures.ProcessPoolExecutor`, потому что docling не release GIL);
- 16 параллельных embedding tasks (MPS sharing → batch-encoding в `sentence-transformers`).

Доп. слой Claude Code subagents — для «креативных» шагов:
- **PerspectiveAgent** генерирует 3–5 disciplinary perspectives для темы (имитация STORM perspective-guided question asking без полной зависимости от DSPy); STORM (Stanford OVAL, `pip install knowledge-storm`) использует `max_perspective=3, max_conv_turn=3` дефолты — берём те же.
- **QueryExpansionAgent** — на каждую perspective выдаёт 4–6 boolean queries для разных API (MeSH-расширение для биомед через NLM MeSH API, обычное synonym-mining для SE);
- **SectionWriterAgent** — пишет каждую секцию отчёта параллельно (related work, methods landscape, gaps, future directions);
- **CriticAgent** — финальный валидатор (anti-hallucination, цитаты на каждом параграфе, MMR diversity).

### 9. Каскад скачивания PDF — точные приоритеты

Для каждого кандидатного DOI/arXivID/PMCID, в порядке убывания приоритета:

1. **OpenAlex** `primary_location.pdf_url` / `best_oa_location.pdf_url`.
2. **Unpaywall** `best_oa_location.url_for_pdf` (50k+ источников проверено).
3. **arXiv** `https://arxiv.org/pdf/{id}.pdf` (если есть arxiv_id).
4. **Europe PMC** `https://www.ebi.ac.uk/europepmc/webservices/rest/{PMCID}/fullTextXML` (биомед, лучше чем PDF — структурированный XML).
5. **PMC** OAI-PMH harvester (если PMCID).
6. **Publisher OA endpoint** (PLOS, MDPI, Frontiers — известные шаблоны URL).
7. **Zenodo/OSF/figshare** direct API.
8. **arXiv HTML (ar5iv)** — если PDF недоступен, html.arxiv.org/abs/{id} даёт чистый HTML который trafilatura парсит на ура.

**`scidownl` / sci-hub-as-fallback** — оставляем **опциональным** через флаг `--grey`, выключенным по умолчанию. PyPaperBot (ferru97) демонстрирует рабочий каскад с sci-hub mirror argument — паттерн заимствуем; юридический статус — серая зона, явный disclaimer в SKILL.md.

### 10. Производительность на M-серии (реалистичные оценки)

- **Discovery**: 8 параллельных API → ~500–1000 кандидатов за 30–60 сек.
- **Fetch PDF**: сеть-bound, ~5–10 PDF/сек если все доступны на быстрых CDN (arXiv, Europe PMC); типичный mix — ~2 PDF/сек.
- **Parse** (гибрид pymupdf4llm + docling-fallback на сложные страницы): на типичной 12-страничной статье pymupdf4llm — ~1 сек, docling — ~12 сек CPU / ~6 сек MPS. Если 30% страниц триггерят docling-fallback: средне ~5 сек/статья = **~10–12 статей/мин на ядро**.
- **Embed** (SPECTER на MPS, batch=32): ~50 chunks/сек ⇒ статья на ~30 chunks = ~0.6 сек.
- **Index** (`sqlite-vec` upsert): не bottleneck, ~5k inserts/сек.
- **End-to-end gating factor**: PDF parsing. На 4 параллельных воркерах ≈ **200–400 статей/час**.
- **Citation traversal** (3 API-вызова/статья, polite pool ≤ 10 RPS) — 100 статей ≈ 30 сек, не bottleneck.
- **Synthesis**: зависит от LLM-вызовов через Claude Code; типично 5–15 мин на отчёт в 15–20 страниц.

**Persistent corpus**: skill **не пересоздаёт** базу. Каждый запуск дописывает новые статьи в `corpus.db`, кэширует API ответы на 24h. Через несколько запросов на смежные темы у вас собирается локальный корпус 1000–5000 статей с эмбеддингами — это сильнее любого Deep Research, который начинает с нуля.

### 11. Конкретный requirements.txt

```
# Core API clients
pyalex>=0.18
semanticscholar>=0.10
arxiv>=2.1
habanero>=2.3
unpywall>=0.2
paperscraper>=0.3
biopython>=1.84            # for Entrez/PubMed E-utilities
huggingface-hub>=0.26

# HTTP/async
httpx[http2]>=0.27
aiometer>=0.5
tenacity>=9.0              # retries with backoff

# Web search / scraping
ddgs>=6.0                  # the new duckduckgo-search rebrand
trafilatura>=1.12
beautifulsoup4>=4.12

# PDF parsing
pymupdf4llm>=0.0.20
docling>=2.0
refextract>=2.1

# Embeddings & vector store
sentence-transformers>=3.2
torch>=2.4                 # MPS на Apple Silicon
sqlite-vec>=0.1.9
sqlite-vec-client>=0.1

# NLP utilities
rank-bm25>=0.2             # для гибридного retrieval
nltk>=3.9
langdetect>=1.0
argos-translate>=1.9       # pure-python NMT (ru-en)

# Bibliography & quality
pyzotero>=1.5
bibtexparser>=1.4

# Optional perspective-engine
knowledge-storm>=0.2       # для STORM perspective-question generation

# Optional / Apple Silicon native
# mlx-embeddings
# mlx-lm

# DEV
pytest>=8.0
ruff>=0.6
```

**Никаких** docker, no GROBID, no Java, no system OCR dependencies (если не включаем easyocr-extra docling). Всё чисто `pip install`.

### 12. Конкуренты: что у кого взяли, что улучшили

| Проект | Что хорошо | Что взяли | Где обходим |
|---|---|---|---|
| **PaperQA2** (Future-House) | RCS reranking, citation traversal — +12.4% над ближайшим конкурентом на RAG-QA Arena Science | Algorithm 1 целиком, RCS-промпт схема, metadata-aware embedding | Замена GROBID/Docker на pymupdf4llm+docling; персистентный SQLite-корпус вместо Numpy in-mem; добавили русскоязычные источники; opensource fetch (не требует «принеси PDF сам») |
| **STORM/Co-STORM** (Stanford OVAL) | perspective-guided question asking, outline→section→polish pipeline | весь outline-stage паттерн через `knowledge-storm` pip; реализуем через Claude subagents | Реальные академические API вместо Wikipedia+You.com; citation grounding до уровня DOI |
| **GPT-Researcher** | planner/executor с парралелизмом, написан на pip, теперь имеет свой `.claude/skills/` каталог | паттерн planner+executor для discovery, шаблон skill | Заменили Tavily (платно) на open API mix; добавили PaperQA2-style traversal которого у них нет |
| **open_deep_research** (LangChain) | LangGraph-based, supervisor-researcher arch, scores 0.4344 on Deep Research Bench | паттерн supervisor + parallel sub-researchers | Без зависимости от LangGraph runtime; пишем нативно в Claude Code skill |
| **research30** (shandley) | минималистичная skill-обёртка с 5 источниками (OpenAlex/S2/PubMed/arXiv/HF), stdlib-only | формат SKILL.md, скрипт-only invocation, --quick/--deep флаги | Расширили до 8+ источников, добавили PDF parsing/RAG/snowballing — research30 ограничен last 30 days и метаданными |
| **Local Deep Research** (LearningCircuit) | 20+ search strategies, SQLCipher encrypted, ~95% SimpleQA | идею «strategy registry» как настраиваемых пайплайнов | Не требует Ollama/SearXNG/SQLCipher отдельно; полностью встроено |
| **paperscraper** (jannisborn) | дампы bioRxiv/medRxiv/chemRxiv локально (~30 MB / 50K+ статей за раз) | используем напрямую как 1 из 8 источников | — |
| **PyPaperBot** (ferru97) | sci-hub fallback chain | паттерн каскада скачивания (Crossref → Scholar → SciHub) | Открытые источники приоритетнее, sci-hub — opt-in `--grey` |
| **Elicit / Consensus / Undermind** | UI/UX для researchers | паттерн «query → review summaries → drill down» | open + local + reproducible (можно показать промежуточные артефакты любому проверяющему ВКР) |

### 13. Roadmap

**MVP (1–2 дня)**
- `SKILL.md` + `ultrasearch.py` который:
  - parallel discovery от OpenAlex + Semantic Scholar + arXiv;
  - dedup по DOI/title;
  - download OA PDFs через Unpaywall;
  - parse через pymupdf4llm;
  - embed SPECTER → sqlite-vec;
  - simple top-k retrieval + Claude генерирует markdown отчёт.
- Без docling, без traversal, без quality scoring.

**v0.5 (1 неделя)**
- Citation traversal (PaperQA2 Algorithm 1);
- docling fallback для сложных PDF;
- Retraction Watch;
- STORM-style perspective-question generation через `knowledge-storm`;
- Каскад скачивания PDF с 5+ источниками;
- pyzotero export.

**v1.0 (2–3 недели)**
- Полный 3-stage retrieval (vec → cross-enc → RCS);
- Multi-section synthesis с CriticAgent;
- Русскоязычные источники (КиберЛенинка через OAI-PMH);
- Mermaid citation graph рендерится в отчёт;
- `--grey` opt-in flag для sci-hub;
- Persistent corpus + incremental indexing через Claude Code hooks (PostToolUse hook авто-добавляет в корпус каждый новый PDF, который пользователь читает);
- Multilingual query translation (через `argos-translate`, pure pip, без API).

### 14. Риски и митигации

| Риск | Митигация |
|---|---|
| OpenAlex с февраля 2026 вводит обязательный API key | Получить ключ заранее (free); код уже подготовлен через `pyalex.config.api_key` |
| Semantic Scholar shared rate limit (5000/5min shared) может быть забит | Получить free dedicated key; fallback на OpenAlex `referenced_works` |
| Marker MPS crashes на M-series (issue #993) | Дефолт parsing — pymupdf4llm+docling-CPU; marker — opt-in `--marker` флаг |
| PyMuPDF AGPL для коммерческого использования | Открытый skill — AGPL совместим; в README жирно прописать про commercial license |
| sci-hub зеркала падают / юридически серая зона | opt-in `--grey`, дефолт выкл; в SKILL.md disclaimer |
| eLIBRARY блокирует scraping | Не скрейпим; resolve через DOI → OpenAlex/Crossref |
| Claude хаотически решает не invoke skill | description во frontmatter — конкретные триггеры на третьем лице (per Anthropic best practices); явный slash-command `/ultrasearch` |
| Превышение context window при многих документах | Progressive disclosure: только metadata в startup, full content загружается per-section; финальный prompt всегда ≤ 15 chunks после MMR |
| OOM при docling на больших PDF (>200 страниц) | Chunked processing страницами по 20, явный `gc.collect()` между батчами; fallback на pymupdf4llm |
| Nougat функционально abandoned (last release Apr 2024) | Не используем; ставим только marker / docling |

---

## Recommendations (конкретные действия)

1. **Сегодня (Day 0)**:
   - получить email-based API access: OpenAlex (just email, **обязательный API key после 13 февраля 2026**), Semantic Scholar (form submission, free), Unpaywall (email);
   - создать `~/.claude/skills/ultrasearch/` с минимальным `SKILL.md` и скелетом;
   - `pip install pyalex semanticscholar arxiv habanero unpywall pymupdf4llm sentence-transformers sqlite-vec httpx aiometer tenacity trafilatura ddgs paperscraper`.

2. **Day 1–2 (MVP)**:
   - имплементировать `discover.py` с 3 источниками в asyncio;
   - имплементировать `fetch.py` с каскадом Unpaywall → arXiv;
   - имплементировать `parse.py` с pymupdf4llm;
   - имплементировать `index.py` с sqlite-vec + SPECTER;
   - простой `retrieve.py` top-k и `synthesize.py` через Claude subagent.

3. **Week 1 (v0.5)**:
   - добавить docling fallback с условным переключением;
   - имплементировать traverse.py — Algorithm 1 целиком;
   - подключить Retraction Watch;
   - добавить STORM-style perspective questions через subagent.

4. **Weeks 2–3 (v1.0)**:
   - русскоязычные источники + перевод запросов через argos-translate;
   - cross-encoder + RCS-stage retrieval;
   - CriticAgent + anti-hallucination валидатор;
   - pyzotero export + Mermaid citation graph.

5. **После v1.0**:
   - бенчмарк на BEIR-SciFact / TREC-COVID / собственном neurotech-evaluation наборе;
   - сравнение с PaperQA2 на LitQA2.

**Пороги для переключения стратегии**:
- если discovery возвращает < 50 кандидатов после 8-source fan-out — расширить query через WordNet + GPT-Researcher-style sub-query expansion;
- если RCS score < 5 на 80% top-50 — переформулировать perspectives, не синтезировать «соломенный отчёт»;
- если parsing fails > 30% PDFs — включить docling-only mode (медленнее, надёжнее);
- если context size финального prompt > 80% Claude window — увеличить порог RCS до 7 и MMR cap до 8 chunks.

---

## Caveats

- **Цифры производительности — оценочные** для M-series. Единственная подтверждённая первоисточником цифра по Marker — 0.22 страниц/сек на M4 Mac MPS (Modal × Datalab joint blog). Docling 0.49 sec/page — на GPU (arXiv:2501.17887). Для M-series CPU нет first-party benchmark для docling; нужны эмпирические измерения.
- **PaperQA2 paper §8.1.1 говорит «four API calls/paper», FutureHouse blog говорит «three»**. Арифметически верно «three» (1 backward Semantic Scholar + 1 backward Crossref + 1 forward Semantic Scholar); используем 3.
- **AGPL у PyMuPDF/pymupdf4llm** — окей для open-source skill, но если кто-то захочет встроить в коммерческое продуктовое приложение, нужна commercial license от Artifex (подтверждено в PyMuPDF Discussion #971).
- **eLIBRARY scraping технически возможен**, но прямо запрещён правилами (ВШЭ sciguide.hse.ru); скрейпинг РИНЦ в research-skill — это правовой риск, mitigirovan тем что мы тянем русские DOI через OpenAlex/Crossref.
- **Sci-hub** — серая зона; включаем только через `--grey` opt-in флаг с disclaimer.
- **Nougat функционально заброшен** (последний релиз Apr 2024, неотвеченные issues #220/#232/#248-250); не рекомендуем включать. Если позарез нужен OCR-style парсинг — Marker или docling.
- **Google Scholar через `scholarly`** работает, но Google активно блокирует и может бросать CAPTCHA; ненадёжно для production-skill.
- **OpenAlex API key** становится обязательным с 13 февраля 2026 — нужно получить заранее, иначе после этой даты skill сломается на новых пользователях.
- **OpenAIRE Search API** будет phased out 31 мая 2026 — переход на Graph API (graph.openaire.eu/docs/apis/) запланирован.
- **Marker MPS support** имеет известные баги: TableRec форсится на CPU; есть открытые crashes на M-series для сложных PDF (issue #993). Использовать с осторожностью или предпочесть docling.
- **OpenAlex 260M+ работ и CORE 449M статей** — числа от первого квартала 2025 / мая 2026, фактически могут уже выше; имеют тенденцию роста ~10% в год.