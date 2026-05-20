# Input: seed below the THETA_SCORE threshold

## Seed

```json
[
  {
    "paper_id": "low_score_seed_001",
    "doi": "10.48550/arXiv.2409.13740",
    "title": "Language agents achieve superhuman synthesis of scientific knowledge",
    "score": 0.5
  }
]
```

## Rationale

This is the short-circuit exercise for the `theta_score` gate in
Algorithm 1. The seed has `score=0.5`, which `traverse_citations` compares
as `0.5 * 10 = 5.0 < 8.0 = THETA_SCORE`. The seed therefore does NOT enter
`qualified_seeds`, and the early-return branch must fire:

```python
qualified_seeds = [s for s in seeds if (s["score"] * 10) >= theta_score]
if not qualified_seeds:
    return {"ok": True, "n_seeds": 0, "n_final": 0,
            "candidates": [], ..., "warnings": ["no_seeds_above_threshold"]}
```

The DOI is the same as the happy-path scenario (PaperQA2) — but because the
score gate fires first, **none of the API code paths should execute**.
Specifically:

- No `_s2_references` call (would burn the S2 5000/5min budget)
- No `_crossref_references` call
- No `_s2_citations` call
- No centroid computation (it's downstream of the gate)
- No SPECTER embedding generation
- No `persist_citation_edges` call (would add spurious rows to `citations`)

This fixture guards against a common refactoring bug: developers move the
score filter from the top of the function down into the FilterOverlap step
"to simplify the control flow" — and accidentally pay the full API cost for
seeds that were never going to qualify. The cost matters: a 20-seed call
with all seeds below threshold should make ZERO API calls, not 60.

This scenario also validates the `api_calls` accounting dict: it must be
present in the result and contain `{"s2_refs": 0, "s2_citers": 0,
"crossref": 0}` exactly (initialised but never incremented).
