# Rubric: 02_arxiv_id_paperqa2

## Pass criteria (all must hold)
- [ ] Process exits 0
- [ ] No source raises
- [ ] arXiv source returns >= 1 raw candidate
- [ ] Dedup output contains exactly one candidate with `arxiv_id == "2409.13740"`
- [ ] That candidate's `title` contains the substring `language agents` OR `paperqa2` (case-insensitive)
- [ ] No two candidates in the dedup output share the same `casefold_title(title)`
- [ ] If S2 also resolved the arXiv ID, its record was merged into the arXiv record (not emitted as a separate candidate)
- [ ] arXiv candidate retains its `arxiv_id` field after dedup (not overwritten by a sparser sibling)

## How to run
`python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py discover.py --query "2409.13740" --json --max-per-source 50`

## Notes
The PaperQA2 paper may or may not have a journal DOI yet — at time of writing it is a
preprint. If a published-venue DOI later appears in OpenAlex/S2, the merged record
should carry both the `arxiv_id` and the `doi` (union of fields, not replacement).
The arXiv ID-shortcut is implementation-sensitive: a regex that matches too eagerly
will hijack keyword queries that happen to contain a 4-digit-dot-4-digit substring
(e.g. version numbers, page ranges). Confirm that running the same query as a free-text
search through OpenAlex/S2 does not crash and does not produce a spurious duplicate.
