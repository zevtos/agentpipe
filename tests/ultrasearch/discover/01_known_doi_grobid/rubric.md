# Rubric: 01_known_doi_grobid

## Pass criteria (all must hold)
- [ ] Process exits 0
- [ ] No source raises; arXiv returning 0 candidates is acceptable
- [ ] OpenAlex returns >= 1 raw candidate
- [ ] Semantic Scholar returns >= 1 raw candidate
- [ ] Dedup output contains a candidate whose `doi` (after `normalize_doi`) equals `10.1145/2380718.2380723`
- [ ] That candidate's `title` contains the substring `grobid` (case-insensitive)
- [ ] That candidate has a non-null `openalex_id` (Work id, e.g. `W…`)
- [ ] That candidate's `referenced_works` array is present and non-empty (required for Stage 2 traversal)
- [ ] No two candidates in the dedup output share the same `normalize_doi(doi)` value
- [ ] No two candidates in the dedup output share the same `casefold_title(title)` value when both have null DOI

## How to run
`python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py discover.py --query "GROBID 10.1145/2380718.2380723" --json --max-per-source 50`

## Notes
This scenario stresses the two-route collision case: the DOI inside the query string
resolves directly via OpenAlex/S2 DOI endpoints, while the `GROBID` token triggers
keyword search on the same sources. Both routes return the same paper, so the dedup
key must collapse them. If you see two candidates with identical `casefold_title` but
one missing a DOI, the merge step is failing to backfill the DOI from the keyword-hit
record. Also verify that `referenced_works` is populated on the merged record — if the
DOI-resolved variant has it but the keyword variant doesn't, the merge must prefer the
richer record (or union the fields), not overwrite with the sparser one.
