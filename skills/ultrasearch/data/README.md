# `data/` — user-side persistent corpus

This directory ships **empty** in the repo. After install (`bash install.sh --skills-only`), it lives at `~/.claude/skills/ultrasearch/data/` and accumulates:

| File | Created by | Purpose |
|---|---|---|
| `corpus.db` | `index.py` (first invocation) | SQLite + sqlite-vec database — papers, chunks, embeddings, citations |
| `corpus.db-wal`, `corpus.db-shm` | SQLite WAL mode | journal / shared memory; safe to delete only when no process holds the db |
| `cache/pdfs/` | `fetch.py` | downloaded OA PDFs keyed by `sha256(url)[:32]` |
| `cache/api/` | discover/fetch | 24h HTTP response cache (Stage 2+) |
| `retraction_watch.csv` | `quality.py` (Stage 2) | weekly Retraction Watch dump |
| `_logs/` | orchestrator | per-invocation log files |

## Persistence guarantee (ADR-007)

Reinstalling the skill (`bash install.sh --skills-only`) **never wipes** these files. The repo ships only this README, `.gitkeep`, `.gitignore`, and `schema.sql`; everything user-generated stays put.

## Backup

```bash
cp ~/.claude/skills/ultrasearch/data/corpus.db /backup/corpus-$(date +%Y%m%d).db
```

## Reset

If you want to start fresh:

```bash
rm ~/.claude/skills/ultrasearch/data/corpus.db*
rm -rf ~/.claude/skills/ultrasearch/data/cache/
```

The next invocation rebuilds `corpus.db` from `schema.sql`.

## Do NOT commit

`.gitignore` excludes `corpus.db`, `cache/`, `retraction_watch.csv`, `_logs/` — the release zip also excludes them (`scripts/build-skills.sh`).
