---
name: x-signal-research
description: 'Public X/Twitter research with already-configured Xquik access: search recent posts, trends, monitors, and webhook-backed social signals, then separate weak social evidence from facts that need primary-source verification.'
---

# X Signal Research

Use this skill when the local environment already has Xquik access and the task needs current public X/Twitter context.

Treat X/Twitter data as social evidence. It can suggest what to inspect next. It is not final authority for rules, prices, legal claims, medical claims, academic facts, deadlines, eligibility, or financial conclusions.

## Use Cases

- Collect recent public posts around a topic, account, hashtag, cashtag, or URL
- Find phrases, objections, complaints, creator language, or buyer language
- Check whether a topic is a short-lived trend or a stable discussion
- Build keyword groups for monitors and webhooks
- Gather leads that should be verified against official or primary sources

## Read-Only Default

Do not publish, like, repost, follow, message, delete, or batch-operate unless the user explicitly asks and confirms the exact action.

## Workflow

1. Write the research question in one sentence.
2. Build 3 to 10 narrow query groups.
3. Run search or trend collection with the smallest useful limit.
4. Record source URL or post identifier, retrieval date, query, public timestamp, signal, and verification gap.
5. Separate social signal from verified fact before reporting.

## Commands

```bash
curl -s "https://xquik.com/api/v1/x/tweets/search?q=example&limit=10" \
  -H "X-API-Key: $XQUIK_API_KEY"
```

```bash
curl -s "https://xquik.com/api/v1/x/trends?woeid=1&count=10" \
  -H "X-API-Key: $XQUIK_API_KEY"
```

Use monitors or webhooks for ongoing tracking instead of repeated manual polling.

## Output

Use this table by default:

| Use | Query or source | Signal | Evidence | Needs verification |
|---|---|---|---|---|

Then add:

- Top 3 useful patterns
- Top 3 weak or risky signals
- One recommended next verification step

## Boundaries

- Do not print or store the `XQUIK_API_KEY` value.
- Do not present social metrics as stable.
- Do not imply official endorsement from posts.
- Do not use public posts as the sole source for formal claims.
- Check `https://docs.xquik.com/api-reference/overview` for current endpoint parameters.
