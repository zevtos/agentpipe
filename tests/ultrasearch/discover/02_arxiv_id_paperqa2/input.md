# Input: arXiv-id query

## Query

```
2409.13740
```

## Rationale

This query is a bare arXiv identifier — the PaperQA2 paper "Language agents achieve
superhuman synthesis of scientific knowledge" (Skarlinski et al., 2024). It exercises
the arXiv source's ID-resolution shortcut: when the query matches the arXiv ID regex
(`\d{4}\.\d{4,5}`), the source must hit the `id_list` endpoint directly rather than
running a full-text search.

The OpenAlex and Semantic Scholar sources will treat the same input as a keyword query.
S2 indexes arXiv IDs and typically resolves them; OpenAlex may or may not, depending
on whether the work has been ingested. Either way, the dedup key for the arXiv-resolved
record is the title (since the arXiv source may not have a DOI for unpublished
preprints), and any cross-source duplicates must collapse via
`casefold_title(title)`.

This fixture validates two Stage 1 properties at once: (a) the arXiv source handles bare
IDs correctly without crashing on the non-keyword input, and (b) the dispatcher
produces exactly one merged candidate even when one source resolves by ID and the
others resolve by keyword.
