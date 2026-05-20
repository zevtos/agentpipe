# Citation traversal — methodology and citation card

Stage 2 lazy-loaded reference. The skill replicates PaperQA2's RCS-driven citation walk, which itself automates Wohlin's snowballing procedure. This doc lays out the academic lineage so users can cite the approach in a thesis or methods section, and so future maintainers know which knobs in `traverse.py` map back to which paper.

## Wohlin 2014 snowballing procedure

Citation: Wohlin, C. (2014). *Guidelines for snowballing in systematic literature studies and a replication in software engineering*. EASE '14, ACM. **DOI: [10.1145/2601248.2601268](https://doi.org/10.1145/2601248.2601268)**. Open PDF: <https://www.wohlin.eu/ease14.pdf>.

Wohlin formalised snowballing as an alternative to database-keyword search for systematic literature reviews. The procedure has three stages:

1. **Start set** — a small seed (~10-20 papers) picked by hand from diverse venues to dodge publisher bias. Wohlin recommends using Google Scholar with curated keywords for the seed, then never relying on it again.
2. **Backward snowballing** — read each seed's reference list; admit a referenced paper if title, abstract, venue, and author profile pass the inclusion criteria.
3. **Forward snowballing** — find papers that cite each seed (Wohlin used Google Scholar's "Cited by"); apply the same inclusion gates.

The crucial step is **iteration**: every newly admitted paper itself becomes a seed for the next pass. The walk terminates when a full backward+forward sweep yields zero new admissions. Wohlin shows on a replicated SE study that snowballing reaches the same final set as keyword search with fewer false positives, provided the start set is diverse.

ultrasearch automates this loop with API calls (Semantic Scholar `/references`, `/citations`, Crossref `reference[]`) instead of manual reading. Inclusion criteria are replaced by a SPECTER cosine gate (semantic) and the FilterOverlap step (popularity-weighted top-k cap).

## PaperQA2 Algorithm 1 (RCS-driven traversal)

Citation: Skarlinski, M. D. et al. (2024). *Language agents achieve superhuman synthesis of scientific knowledge*. **[arXiv:2409.13740](https://arxiv.org/abs/2409.13740)**, §8.1.1. FutureHouse blog: <https://www.futurehouse.org/research-announcements/paperqa2>.

PaperQA2 wraps Wohlin's loop in an LLM-scored gate. The pseudocode (research §3 lines 56-61):

```
D_prev = {s.d for s in S if s.score >= theta_score}
D      = GetCitations(D_prev, fut)        # one degree only
theta_o = math.ceil(alpha * len(D))
return FilterOverlap(D, D_prev, theta_o, ell)
```

* `S` — current seed set; each seed `s` has a paper `s.d` and a Relevant Context Score `s.score` (0-10 scale, LLM-judged).
* `theta_score = 8` — only seeds scoring ≥ 8 propagate citations. Stops irrelevant seeds from poisoning the walk.
* `GetCitations(D_prev, fut=True)` — fetch one degree of backward refs and (if `fut`) forward citers.
* `alpha = 1/3` — overlap-filter fraction.
* `theta_o = ceil(alpha * |D|)` — minimum number of seeds a candidate must be cited by to survive (in Stage 2 we sort by `cited_by_count` as proxy; per-seed edge attribution is deferred to Stage 3).
* `ell = 12` — hard cap on candidates returned per call.

**API budget**: 3 calls per seed = 1× S2 `/references` + 1× Crossref `reference[]` + 1× S2 `/citations`. A 20-seed traversal therefore costs 60 HTTP calls (~9 s wall-clock with per-domain rate budgets in `traverse.py:75-79`). Research §3 line 57 notes that the PaperQA2 paper text says "four API calls" but the arithmetic only supports three; ultrasearch uses three and matches the arithmetic.

FutureHouse reports a **12.4 % improvement over the closest competitor on RAG-QA Arena Science** for the full PaperQA2 pipeline — credit FutureHouse blog (link above), not the arXiv paper, which gives different aggregate numbers.

`--depth default` runs one hop; `--depth deep` recurses with `max_hops=2` and synthetic seed scores of 0.85 for survivors of the cosine gate.

## SPECTER cosine gate

Citations: Cohan, A., Feldman, S., Beltagy, I., Downey, D., & Weld, D. S. (2020). *SPECTER: Document-level representation learning using citation-informed transformers*. **[arXiv:2004.07180](https://arxiv.org/abs/2004.07180)**. Ostendorff, M. et al. (2022). *Neighborhood Contrastive Learning for Scientific Document Representations* (SciNCL). **[arXiv:2202.06671](https://arxiv.org/abs/2202.06671)**.

Each candidate `(title + abstract)` is embedded with `allenai-specter` (Hugging Face), L2-normalised, then dotted against the **mean of all chunk embeddings already in `corpus.db`** (the corpus centroid; `traverse.py:129-163`). A candidate survives iff the cosine similarity ≥ **0.55**.

The 0.55 threshold comes from SciNCL's reported nearest-neighbour cutoff for in-domain papers (research §3 line 71). Below it, candidates are usually topical drift — Wohlin's manual inclusion criteria, but as a semantic gate.

If `corpus.db` is empty, the gate is skipped with a warning (`empty_corpus_centroid_skipping_specter_gate`).

## Stage-specific status

**Stage 2 (current)** — Algorithm 1 is implemented in `traverse.py`. `--depth default` = 1 hop (Wohlin's "iteration 1"). `--depth deep` = 2 hops. The RCS threshold is enforced against `retrieve.Hit.score × 10` because Stage 2 still uses BM25-rerank scores in [0, 1] rather than LLM-judged 0-10 RCS.

**Stage 3 (planned)** — Add a cross-encoder reranker plus an LLM-scored RCS as a true third retrieval stage. That LLM score is what the PaperQA2 paper actually means by "score ≥ 8"; Stage 2 ultrasearch approximates it via `retrieve.Hit.score`, which is correlated but not identical.

## How to cite ultrasearch

For thesis-level use, cite the lineage:

* Wohlin (2014) — DOI 10.1145/2601248.2601268
* PaperQA2 / Skarlinski et al. (2024) — arXiv:2409.13740
* SPECTER / Cohan et al. (2020) — arXiv:2004.07180
* SciNCL / Ostendorff et al. (2022) — arXiv:2202.06671
* ultrasearch (this skill) — `https://github.com/zevtos/agentpipe` (skill `skills/ultrasearch/`)

## References

Cohan, A., Feldman, S., Beltagy, I., Downey, D., & Weld, D. S. (2020). SPECTER: Document-level representation learning using citation-informed transformers. *ACL 2020*. arXiv:2004.07180.

Ostendorff, M., Rethmeier, N., Augenstein, I., Gipp, B., & Rehm, G. (2022). Neighborhood contrastive learning for scientific document representations with citation embeddings (SciNCL). *EMNLP 2022*. arXiv:2202.06671.

Skarlinski, M. D., Cox, S., Laurent, J. M., Braza, J. D., Hinks, M., Hammerling, M. J., Ponnapati, M., Rodriques, S. G., & White, A. D. (2024). Language agents achieve superhuman synthesis of scientific knowledge. arXiv:2409.13740.

Wohlin, C. (2014). Guidelines for snowballing in systematic literature studies and a replication in software engineering. *EASE '14*. ACM. DOI: 10.1145/2601248.2601268.
