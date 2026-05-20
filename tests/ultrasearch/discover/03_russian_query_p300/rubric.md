# Rubric: 03_russian_query_p300

## Pass criteria (all must hold)
- [ ] Process exits 0
- [ ] No source raises on the Cyrillic query (no UnicodeEncodeError, no httpx URL error)
- [ ] At least one of {OpenAlex, S2} returns >= 1 raw candidate
- [ ] arXiv returning 0 candidates is acceptable (not a failure)
- [ ] Dedup output contains >= 1 candidate
- [ ] All emitted candidates have a non-empty `title` field
- [ ] No two candidates share the same `normalize_doi(doi)` or `casefold_title(title)` key
- [ ] Any Cyrillic title in the output has been NFC-normalised before casefolding (verify by re-running `casefold_title` on the emitted title and confirming the key matches what dedup used)

## How to run
`python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py discover.py --query "нейроинтерфейсы P300" --json --max-per-source 50`

## Notes
This is the Unicode-safety canary. If any source raises on the Cyrillic input, the bug
is almost certainly in query construction (forgot to `quote()` the param, or passed
`str` where `bytes` was expected). If candidates come back but dedup over-counts,
suspect the casefold path: titles like `"ЭЭГ"` and `"ээг"` must collapse to the same
key, which requires `unicodedata.normalize("NFC", t).casefold()` rather than plain
`t.lower()`. A zero-result run from all three sources is suspicious — the Latin "P300"
token alone should match thousands of papers — and likely indicates that the Cyrillic
prefix is mangling the URL rather than degrading gracefully to a partial match.
