# Rubric: 03_no_doi_seed

## Pass criteria (all must hold)
- [ ] Process exits 0
- [ ] `ok == true` in the emitted JSON
- [ ] `n_seeds == 1` (the PaperQA2 seed qualified; the no-DOI seed was skipped)
- [ ] `warnings` contains at least one entry containing the substring `seed has no DOI` (case-insensitive) AND identifying the skipped seed (by paper_id or title)
- [ ] `warnings` does NOT contain `no_seeds_above_threshold` (one seed proceeded)
- [ ] `api_calls.s2_refs >= 1` (the surviving PaperQA2 seed triggered S2)
- [ ] `api_calls.crossref >= 1`
- [ ] `api_calls.s2_citers >= 1`
- [ ] `n_final >= 1` (the PaperQA2 seed produced at least one candidate, per scenario 01)
- [ ] Every candidate in `candidates[]` has `parent_paper_id == "paperqa2_2409_13740"` (only the surviving seed pulled candidates)
- [ ] NO candidate has `parent_paper_id == "russian_preprint_no_doi_001"` (the skipped seed contributed zero)
- [ ] Process does NOT crash with `TypeError`, `AttributeError: 'NoneType'`, `UnicodeEncodeError`, or `json.JSONDecodeError` on the Cyrillic title
- [ ] The Cyrillic title in the warning message (if echoed) is preserved verbatim (NFC-normalised) — not mojibake (`Ð...`) or escape sequences (`Н...`) in human-facing log output

## How to run
```
python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py traverse.py \
  --seeds-json '[{"paper_id":"russian_preprint_no_doi_001","doi":null,"title":"Нейроинтерфейсы на основе P300: обзор современных подходов","score":0.85},{"paper_id":"paperqa2_2409_13740","doi":"10.48550/arXiv.2409.13740","title":"Language agents achieve superhuman synthesis of scientific knowledge","score":0.85}]' \
  --max-hops 1 --json
```

Optional: also test the "all seeds lack DOI" sub-case by passing only the
first seed:
```
python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py traverse.py \
  --seeds-json '[{"paper_id":"russian_preprint_no_doi_001","doi":null,"title":"Нейроинтерфейсы на основе P300: обзор современных подходов","score":0.85}]' \
  --max-hops 1 --json
```
For that sub-case, `n_seeds == 0`, `n_final == 0`, and warnings contain BOTH
`seed has no DOI` and (implicitly) the empty-result short-circuit — but
notably NOT `no_seeds_above_threshold`, because the score gate did pass; the
seeds were dropped at a later step.

## Notes
The most likely failure modes:

1. **Whole traversal aborts on the first no-DOI seed** — symptom: `n_seeds
   == 0` and no candidates returned. Bug: developer used `return` instead of
   `continue` in the seed-resolution loop. Fix: `_paper_id_to_seed_doi`
   returning `None` should append to warnings and `continue` to the next
   seed.
2. **Skipped seed silently** — the run produces correct candidates from the
   PaperQA2 seed but no `seed has no DOI` warning is emitted. The caller has
   no way to know why their input produced fewer seeds than expected. Fix:
   ensure the skip path appends to `warnings` with a paper_id or title
   identifier.
3. **JSON decode error on Cyrillic input** — if `--seeds-json` doesn't
   handle UTF-8 properly (e.g. wrapped in `subprocess.run` without
   `encoding='utf-8'`), the Cyrillic title arrives as mojibake or raises
   `UnicodeDecodeError`. Fix: ensure stdin/argv reads use `encoding='utf-8'`
   (`sys.argv` is already unicode in Python 3 on most platforms; the issue
   is usually in how shell quoting passes the string).
4. **`parent_paper_id` collision** — if the dedup step collapses candidates
   from two seeds into one record but loses track of which seed originally
   pulled it, the `parent_paper_id` field may point to a different seed than
   the one that actually contributed the candidate. For this fixture, there
   should be no collision risk (only one seed contributes), but verify via
   the assertion that ALL candidates have `parent_paper_id ==
   "paperqa2_2409_13740"`.
5. **Lookup-by-paper_id false positive** — `_paper_id_to_seed_doi` queries
   the `papers` table for the seed's paper_id and returns its DOI. If the
   synthetic `russian_preprint_no_doi_001` happens to collide with a real
   paper_id in the corpus (extremely unlikely with the prefix used), the
   skip path would not fire. Pick a paper_id prefix unlikely to clash; the
   `russian_preprint_no_doi_` prefix is suitably unique.
