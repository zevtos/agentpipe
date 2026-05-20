# Input: seed without a DOI (mixed with a valid seed)

## Seeds

```json
[
  {
    "paper_id": "russian_preprint_no_doi_001",
    "doi": null,
    "title": "Нейроинтерфейсы на основе P300: обзор современных подходов",
    "score": 0.85
  },
  {
    "paper_id": "paperqa2_2409_13740",
    "doi": "10.48550/arXiv.2409.13740",
    "title": "Language agents achieve superhuman synthesis of scientific knowledge",
    "score": 0.85
  }
]
```

## Rationale

This fixture exercises the `seed has no DOI` skip path documented in
§3.1.7 of the Stage 2 plan:

> | Seed has no DOI | Skip that seed; log warning; orchestrator already
> filtered seeds via `_paper_id_to_seed_doi` |

The first seed is a synthetic Russian-language preprint with no DOI assigned —
a realistic case for samizdat preprints, unpublished conference proceedings,
or older Russian/Eastern-European literature that never received a Crossref
DOI. Its `paper_id` is also synthetic so `_paper_id_to_seed_doi(con,
paper_id)` returns `None` (no row in `papers` table to look up).

The second seed is the canonical PaperQA2 paper with a valid DOI, identical
to scenario 01. With both seeds in the same call, the expected behaviour is:

1. **Seed 1 (no DOI)** — traversal logs a warning `seed has no DOI` (or the
   exact wording specified in the plan) and skips it. No S2 or Crossref call
   is made for this seed.
2. **Seed 2 (PaperQA2)** — traversal proceeds normally, hitting S2 (refs +
   citers) and Crossref (refs), producing candidates as in scenario 01.

This validates **graceful degradation**: the presence of one bad seed must
not abort the whole traversal. The Stage 2 plan is explicit (§3.1.7) that
this case is "common" — many real corpora contain papers without DOIs,
especially in non-English scholarship — so the skip-and-continue path must
be the default behaviour, not an error.

The Cyrillic title also exercises Unicode safety at a different layer than
scenario 03 of the discover suite: here the title is part of a JSON
seed-input payload, so the test confirms that `json.loads` of the
`--seeds-json` argument preserves the Cyrillic characters losslessly and
that the title-norm/short-id paths downstream don't crash on non-ASCII.

This fixture catches: a) regression where missing-DOI causes a `TypeError:
'NoneType' has no attribute 'lower'` somewhere in the seed-resolution path,
b) regression where the skip warning is incorrectly emitted for ALL seeds
(short-circuit returns too early instead of continuing the loop), and c)
silent skip of the bad seed without a warning (caller can't diagnose why
their seed produced no candidates).
