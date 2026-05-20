# Input: PaperQA2 high-score seed (happy path)

## Seed

```json
[
  {
    "paper_id": "paperqa2_2409_13740",
    "doi": "10.48550/arXiv.2409.13740",
    "title": "Language agents achieve superhuman synthesis of scientific knowledge",
    "score": 0.85
  }
]
```

## Rationale

This is the canonical happy-path exercise for `traverse.py`'s implementation of
PaperQA2 Algorithm 1 (research §3 lines 63-69). The seed is the PaperQA2 paper
itself (arXiv:2409.13740, DOI `10.48550/arXiv.2409.13740`) which is well-indexed
by both Semantic Scholar and Crossref and has a substantial reference list plus
forward citers — so every API call along the traversal path returns non-empty
results.

The seed's `score=0.85` is intentionally above the `THETA_SCORE=8.0` gate
(compared as `score * 10 >= theta_score`, so `8.5 >= 8.0`). This means
`qualified_seeds` is non-empty and Algorithm 1 proceeds past the
`no_seeds_above_threshold` short-circuit.

The fixture exercises four pipelines simultaneously:

1. **Backward references via Semantic Scholar** — `_s2_references` hits
   `/paper/{DOI:<doi>}/references` and must return >= 10 records with the
   `externalIds,title,authors,year,venue,citationCount,abstract,openAccessPdf,paperId`
   field selection.
2. **Backward references via Crossref** — `_crossref_references` hits
   `https://api.crossref.org/works/{doi}` and pulls `message.reference[].DOI`.
   Many will overlap with S2's list but a non-empty subset are Crossref-only
   (older venue records, books, etc.).
3. **Forward citers via Semantic Scholar** — `_s2_citations` hits
   `/paper/{DOI:<doi>}/citations` and must return >= 5 records (PaperQA2 has
   been cited widely since publication).
4. **Dedup, SPECTER gate, and FilterOverlap** — the raw candidate set is
   deduped by `_dedup_traversal` (DOI-or-title key), the centroid of the
   corpus is computed, embeddings are generated for each candidate, the
   cosine ≥ 0.55 gate fires, and `filter_overlap` keeps survivors that pass
   `theta_o = ceil(ALPHA * |D|)` with `ALPHA = 1/3` and the `ELL = 12` cap.

The traversal must also persist edges to the `citations` table via
`persist_citation_edges` inside `writer_tx`, with `INSERT OR IGNORE` so
re-running the same seed adds zero new rows (idempotence).

This fixture catches: API-key handling under `S2_API_KEY`/`CROSSREF_EMAIL`,
the aiometer rate-budget configuration (`s2_refs=1.0`, `crossref=5.0`,
`s2_citers=1.0`), centroid computation from `vec_chunks` embeddings, the
SPECTER cosine gate threshold, and the FilterOverlap cap at `ell=12`.
