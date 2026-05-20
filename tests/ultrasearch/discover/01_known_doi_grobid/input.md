# Input: known-DOI English query

## Query

```
GROBID 10.1145/2380718.2380723
```

## Rationale

This query exercises `discover.py` along two orthogonal axes simultaneously:

1. **DOI-resolution path** — the embedded DOI `10.1145/2380718.2380723` points to Patrice
   Lopez's "GROBID: Combining Automatic Bibliographic Data Recognition and Term Extraction
   for Scholarship Publications" (TPDL 2009). OpenAlex and Semantic Scholar both expose
   direct DOI lookups, so each source should resolve to the exact paper.
2. **Keyword-search path** — the bare token `GROBID` forces the same sources to also run a
   full-text search; the DOI-resolved record and the keyword-search top hit MUST collapse
   into a single candidate under the Stage 1 dedup rule
   (`normalize_doi(doi) or casefold_title(title)`). This validates that the dispatcher does
   not emit duplicate entries when a query yields the same paper through two routes.

GROBID is also the PDF parser cited by PaperQA2, so it is a realistic seed for the
downstream Stage 2 reference traversal — the resolved candidate must carry an
`openalex_id` and a populated `referenced_works` array for that traversal to work.
arXiv is not expected to return a hit (TPDL is a conference, not a preprint venue), and
a 0-candidate arXiv response is the correct behaviour, not a failure.
