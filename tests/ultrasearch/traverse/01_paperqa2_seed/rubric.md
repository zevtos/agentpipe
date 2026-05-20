# Rubric: 01_paperqa2_seed

## Pass criteria (all must hold)
- [ ] Process exits 0
- [ ] `ok == true` in the emitted JSON
- [ ] `n_seeds == 1` (the seed was above threshold and resolved to a DOI)
- [ ] `api_calls.s2_refs >= 1` AND `api_calls.crossref >= 1` AND `api_calls.s2_citers >= 1` (all three families fired)
- [ ] S2 backward references returned >= 10 raw candidates (before dedup)
- [ ] Crossref backward references returned >= 5 raw candidates (older venue DOIs not in S2)
- [ ] S2 forward citers returned >= 5 raw candidates
- [ ] `n_raw_candidates >= 15` (union of all three families before dedup)
- [ ] After dedup, `n_after_specter_gate >= 3` (some candidates survive the cosine ≥ 0.55 gate)
- [ ] `n_final >= 3` AND `n_final <= 12` (passed FilterOverlap, capped at ELL)
- [ ] Each candidate in `candidates[]` has `discovered_via_traversal == true`
- [ ] Each candidate has a valid `direction` (`"forward"` or `"backward"`)
- [ ] Each candidate has a valid `source_api` (`"s2"` or `"crossref"`)
- [ ] Each candidate has `parent_paper_id == "paperqa2_2409_13740"` (the seed)
- [ ] Each candidate has a populated `cosine_to_centroid` field with value >= 0.55
- [ ] `citations` table in `corpus.db` gained >= 10 new rows after the run
- [ ] No two candidates in `candidates[]` share the same `normalize_doi(doi)` value (dedup works)
- [ ] `warnings` does NOT contain `no_seeds_above_threshold` or `seed has no DOI`
- [ ] `warnings` does NOT contain `empty_corpus_centroid_skipping_specter_gate` (corpus must be non-empty for this scenario)
- [ ] Re-running the same command adds 0 new rows to `citations` (idempotence via `INSERT OR IGNORE`)

## How to run
```
python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py traverse.py \
  --seeds-json '[{"paper_id":"paperqa2_2409_13740","doi":"10.48550/arXiv.2409.13740","title":"Language agents achieve superhuman synthesis of scientific knowledge","score":0.85}]' \
  --max-hops 1 --json
```

Precondition: the seed paper must already exist in `corpus.db` with at least
some indexed chunks in `vec_chunks` (so `compute_corpus_centroid` returns a
non-None vector). If the corpus is empty, run `ultrasearch.py "PaperQA2"`
first to seed it.

## Notes
This is the canonical end-to-end exercise for Algorithm 1. The most likely
failure modes:

1. **S2 anonymous-bucket throttle** — without `S2_API_KEY`, S2 may return 429
   under aiometer's 1 RPS budget if other ultrasearch runs recently consumed
   the 5000/5min bucket. Set `S2_API_KEY` before running for reliable results.
2. **Empty `citations` table after run** — `persist_citation_edges` is called
   inside `writer_tx`, so if the transaction is rolled back (e.g. UNIQUE
   constraint violation surfacing instead of being silenced by `INSERT OR
   IGNORE`), zero rows persist. Check the SQL uses `INSERT OR IGNORE INTO
   citations(...)`.
3. **SPECTER gate over-rejecting** — if `n_after_specter_gate == 0` despite
   many raw candidates, the centroid is likely degenerate (e.g. computed
   from non-normalised vectors). Verify `compute_corpus_centroid` normalises
   by `np.linalg.norm` before return.
4. **FilterOverlap rejecting everything** — `theta_o = ceil(1/3 * len(D))`.
   With `len(D) ~= 20`, `theta_o = 7`. If every candidate is only cited by
   the single seed (overlap = 1), they all fail. This is correct behaviour
   for a 1-seed traversal — the fixture relaxes by requiring `n_final >= 3`,
   accepting that some survivors may come from tie-break by future-citers
   count when overlap is below `theta_o` (Stage 2 design note: see §3.1.2
   `filter_overlap` docstring).
5. **Hot re-run not idempotent** — if the second run adds rows, the UNIQUE
   constraint `UNIQUE(paper_id, cited_doi, direction)` is missing from the
   `citations` schema or the INSERT path is using plain `INSERT` instead of
   `INSERT OR IGNORE`.
