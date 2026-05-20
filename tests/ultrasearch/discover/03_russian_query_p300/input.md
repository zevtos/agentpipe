# Input: Russian-language query

## Query

```
нейроинтерфейсы P300
```

## Rationale

This query mixes Cyrillic ("нейроинтерфейсы" — "neural interfaces") with the Latin
token "P300" (the well-known event-related potential used in BCI spellers). Stage 1 has
no Russian-language sources — OpenAlex, Semantic Scholar, and arXiv all index
predominantly English literature — but the dispatcher MUST NOT crash when handed
non-ASCII input, and the URL-encoding layer must produce valid query strings.

The expected behaviour: English-language papers on P300 BCIs that have been
cross-indexed under the Cyrillic keyword (or that S2/OpenAlex's multilingual
tokenizers happen to surface via the Latin "P300" token alone) should appear in the
results. The fixture is intentionally tolerant — we require at least one candidate
in the union of OpenAlex and S2, but accept any reasonable subset.

This scenario is the canary for two classes of bug: (a) `urllib.parse.quote` or
`httpx` mis-handling of UTF-8 bytes in query parameters, and (b) downstream code that
assumes ASCII-only titles when computing `casefold_title` for dedup. If a source
returns a Cyrillic-titled record, the casefold step must NFC-normalise before
lowercasing, or two visually-identical titles will hash to different keys.
