# Rubric: 02_below_threshold

## Pass criteria (all must hold)
- [ ] Process exits 0
- [ ] `ok == true` in the emitted JSON
- [ ] `n_seeds == 0` (no seed passed the THETA_SCORE gate)
- [ ] `n_final == 0` (no candidates produced)
- [ ] `n_raw_candidates == 0` OR the field is absent (no API calls were made)
- [ ] `candidates == []` (empty list)
- [ ] `api_calls.s2_refs == 0` AND `api_calls.crossref == 0` AND `api_calls.s2_citers == 0` (no API calls fired)
- [ ] `warnings` contains the string `no_seeds_above_threshold`
- [ ] `elapsed_s < 1.0` (no network I/O occurred — early return is fast)
- [ ] `citations` table row count is unchanged before vs after running this command
- [ ] `papers` table row count is unchanged (traverse never writes to `papers` directly)
- [ ] No HTTP requests appear in any debug log emitted to stderr (verify with `--debug` if available, or by setting `HTTPX_LOG_LEVEL=debug` and grepping stderr for `Request`)

## How to run
```
python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py traverse.py \
  --seeds-json '[{"paper_id":"low_score_seed_001","doi":"10.48550/arXiv.2409.13740","title":"Language agents achieve superhuman synthesis of scientific knowledge","score":0.5}]' \
  --max-hops 1 --json
```

Optional verification — snapshot row counts before/after:
```
sqlite3 ~/.claude/skills/ultrasearch/data/corpus.db \
  "SELECT COUNT(*) FROM citations;" > /tmp/citations_before.txt
# (run the command above)
sqlite3 ~/.claude/skills/ultrasearch/data/corpus.db \
  "SELECT COUNT(*) FROM citations;" > /tmp/citations_after.txt
diff /tmp/citations_before.txt /tmp/citations_after.txt   # must be empty
```

## Notes
This fixture is the negative control. The most likely failure modes:

1. **Score gate uses wrong scale** — if the developer wrote
   `if s["score"] >= theta_score` (comparing 0.5 directly to 8.0) instead of
   `if s["score"] * 10 >= theta_score`, every realistic seed will fail and
   the happy-path test (01) will also fail with zero candidates. Conversely
   if they wrote `if s["score"] >= theta_score / 10` (0.5 >= 0.8 — false,
   correct), or `if s["score"] * 10 > theta_score` (5.0 > 8.0 — false,
   correct), the gate still works but boundary semantics (>= vs >) differ.
   The plan specifies `>=`, so a seed with score exactly 0.8 must qualify.
2. **API calls fire before the gate** — easiest tell: `elapsed_s` jumps from
   ~0.01s to several seconds. The fix is to put the `if not
   qualified_seeds` check BEFORE the `async with httpx.AsyncClient(...)`
   block, not after.
3. **Empty `candidates` but `n_seeds > 0`** — means the seed passed the gate
   but everything downstream filtered it out. That's a different bug
   (probably FilterOverlap or SPECTER gate too aggressive), not the
   threshold one. Re-read the failure to disambiguate.
4. **Warning string drift** — if the warning is `"no_qualifying_seeds"` or
   `"below_threshold"` instead of `"no_seeds_above_threshold"`, the
   downstream orchestrator (`ultrasearch.py`) that switches on this warning
   to emit a user-facing message will silently treat it as an unknown
   warning and skip the user message. Keep the exact string.
