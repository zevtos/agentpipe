# `data/` — user-side persistent corpus

This directory ships only **code** (`schema.sql`, this README, `.gitignore`, `.gitkeep`). At runtime the corpus + caches live in a **global state dir outside the installed code** (ADR-008), keyed by skill name and shared across install targets:

```bash
STATE="${ULTRASEARCH_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/agentpipe/ultrasearch}/data"
```

`$STATE/` accumulates:

| File | Created by | Purpose |
|---|---|---|
| `corpus.db` | `index.py` (first invocation) | SQLite + sqlite-vec database — papers, chunks, embeddings, citations |
| `corpus.db-wal`, `corpus.db-shm` | SQLite WAL mode | journal / shared memory; safe to delete only when no process holds the db |
| `cache/pdfs/` | `fetch.py` | downloaded OA PDFs keyed by `sha256(url)[:32]` |
| `cache/api/` | discover/fetch | 24h HTTP response cache (Stage 2+) |
| `retraction_watch.csv` | `quality.py` (Stage 2) | weekly Retraction Watch dump |
| `_logs/` | orchestrator | per-invocation log files |

## Persistence guarantee (ADR-008)

The corpus lives in a global state dir **outside** the installed code, keyed by skill name. Reinstall, update, and multi-target installs **never** touch it — the installer only ever replaces `~/.claude/skills/ultrasearch/` (code). On first run after upgrading from the legacy in-code layout, `ensure_env.py` **moves** any old `~/.claude/skills/ultrasearch/data/corpus.db` to `$STATE` (move, never rebuild). Supersedes ADR-007, which kept the corpus inside the code dir where reinstall actually wiped it.

## Backup

```bash
cp "$STATE/corpus.db" /backup/corpus-$(date +%Y%m%d).db
```

## Reset

If you want to start fresh:

```bash
rm "$STATE/corpus.db"*
rm -rf "$STATE/cache/"
```

The next invocation rebuilds `corpus.db` from `schema.sql`.

## Do NOT commit

`.gitignore` excludes `corpus.db`, `cache/`, `retraction_watch.csv`, `_logs/` — the release zip also excludes them (`scripts/build-skills.sh`).
