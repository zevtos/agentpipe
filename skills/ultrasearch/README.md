# ultrasearch (agentpipe skill)

Thesis-level literature research for Claude Code / Codex CLI.

Queries open academic APIs (OpenAlex, Semantic Scholar, arXiv), downloads OA PDFs, parses them with `pymupdf4llm`, embeds with `allenai-specter`, stores in a persistent `sqlite-vec` corpus, and synthesizes a structured markdown report with grounded `[Sn]` → DOI citations.

**Status:** Stage 1 MVP (research/16_ultrasearch_claude_code_skill §13). See [SKILL.md](./SKILL.md) for usage, env vars, pipeline diagram, and Stage 1 limits.

**Roadmap:**
- **Stage 1 (MVP)** — discovery, fetch, parse, index, naive retrieval, deterministic synthesis. **Done.**
- **Stage 2 (v0.5)** — citation traversal (PaperQA2 Algorithm 1), docling fallback, Retraction Watch, STORM perspectives, pyzotero export.
- **Stage 3 (v1.0)** — 3-stage retrieval (vec + cross-encoder + RCS), CriticAgent, Russian sources, Mermaid citation graph, `--grey` opt-in.

## Quick start

```bash
# 1. install
bash /Volumes/Dev/agentpipe/install.sh --skills-only

# 2. set env vars
export OPENALEX_API_KEY="..."        # free at https://openalex.org/account
export UNPAYWALL_EMAIL="you@example.com"

# 3. invoke
python3 ~/.claude/skills/ultrasearch/scripts/ensure_env.py ultrasearch.py \
    "SSVEP-based BCIs neural prosthetics" \
    --max-papers 30 \
    --out /tmp/ssvep-review.md
```

First run downloads ~700 MB of dependencies (torch + sentence-transformers + allenai-specter model). Subsequent runs are warm.

## Files

- `SKILL.md` — progressive-disclosure entry, env-var matrix, pipeline diagram
- `scripts/ultrasearch.py` — main CLI orchestrator
- `scripts/discover.py`, `fetch.py`, `parse.py`, `index.py`, `retrieve.py`, `synthesize.py` — pipeline stages
- `scripts/ensure_env.py` — venv bootstrap (vendored from doc2kb)
- `scripts/_common.py` — shared helpers (DOI normalization, casefold title, etc.)
- `data/schema.sql` — SQLite + sqlite-vec DDL
- `references/apis.md`, `references/parsing-troubleshooting.md` — lazy-loaded ops docs

## License

MIT — see [LICENSE](./LICENSE). Transitively depends on AGPL `pymupdf4llm` / `pymupdf`; commercial reuse requires an Artifex license.
