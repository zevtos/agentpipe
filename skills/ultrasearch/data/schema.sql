-- ultrasearch corpus schema (v2 Stage 1 — schema_version 1.3)
-- ============================================================
-- This file is loaded by skills/ultrasearch/scripts/index.py via
-- `con.executescript(open(schema_path).read())` from `_ensure_schema()`.
-- All CREATE statements use IF NOT EXISTS so the file is idempotent
-- and safe to re-run on every connection bootstrap.
--
-- Stage 2/3 evolution policy (read before editing):
--   - Stage 1 corpus.db files in the wild MUST keep working. New columns
--     are added by the Python migration helper in index.py::_ensure_schema()
--     via probe-and-add (`PRAGMA table_info` -> ALTER TABLE ADD COLUMN).
--   - Fresh installs get the columns directly from this CREATE TABLE.
--   - All additive columns are NULLABLE — older writers that don't
--     populate them continue to succeed.
--   - Never DROP a column, never add NOT NULL to an existing table.
--     Schema is strictly additive across stages.
--   - NEW tables (e.g. rcs_scores) just appear via CREATE TABLE IF NOT
--     EXISTS — every executescript() pass adds them on legacy DBs too,
--     so no Python migration is needed for table additions.
--
-- ADR-003: vector store is sqlite-vec 0.1.9 (vec0 virtual table, 768-dim
--          float embeddings — matches allenai-specter output dimension).
-- ADR-007: the materialised corpus.db lives at
--          ~/.claude/skills/ultrasearch/data/corpus.db, NEVER inside the
--          repo. This file ships in-repo, but is consumed only post-install.
--
-- Writer locking discipline (read this before issuing any write):
--   - Every writer MUST open its transaction with `BEGIN IMMEDIATE;` rather
--     than the implicit `BEGIN DEFERRED`. WAL allows concurrent readers but
--     only one writer; `BEGIN IMMEDIATE` acquires the RESERVED lock at
--     transaction start so two concurrent index.py runs serialise cleanly
--     instead of one hitting SQLITE_BUSY mid-transaction after work is done.
--   - `busy_timeout` should be set to ~5000 ms on every connection so
--     short contentions resolve transparently.
--   - The `vec_chunks.rowid == chunks.chunk_id` invariant must be preserved
--     by every writer (see comment on vec_chunks below).
-- ============================================================

PRAGMA journal_mode = WAL;       -- concurrent readers + one writer
PRAGMA synchronous  = NORMAL;    -- WAL-safe, ~3-5x faster than FULL
PRAGMA foreign_keys = ON;        -- enforce FK CASCADE on papers->chunks
PRAGMA temp_store   = MEMORY;    -- keep temp btrees off the user's disk

-- ============================================================
-- papers — one row per deduplicated work
-- ============================================================
-- paper_id is sha256(normalize_doi(doi) or 'T:'+casefold_title)[:16],
-- computed by index.py before insert. Treat it as opaque outside Python.
--
-- Stage 2 columns (now part of the canonical CREATE — fresh installs only;
-- existing corpus.db files receive them via index.py's ALTER TABLE migration):
--   retracted_at  TEXT  — ISO-8601 retraction date from Retraction Watch
--                         (populated by quality.py via JOIN on retractions.doi)
--   h_index       REAL  — primary-author h-index from OpenAlex authorships
--   q_score       REAL  — composite quality score (Stage 2 seed,
--                         Stage 3 refines with translation + institution prior)
--
-- Stage 3 columns (now part of the canonical CREATE — fresh installs only;
-- existing corpus.db files receive them via index.py's ALTER TABLE migration):
--   abstract_translated TEXT  — argos-translate output; for RU candidates we
--                               store the EN translation here. NULL for papers
--                               whose abstract is already English (or absent).
--   institution         TEXT  — primary author affiliation, used by the
--                               Stage 3 diversity cap (max papers per
--                               institution per ranked result set).
-- ============================================================
CREATE TABLE IF NOT EXISTS papers (
    paper_id              TEXT PRIMARY KEY,
    doi                   TEXT,
    arxiv_id              TEXT,
    openalex_id           TEXT,
    s2_paper_id           TEXT,
    title                 TEXT NOT NULL,
    title_norm            TEXT NOT NULL,
    abstract              TEXT,
    year                  INTEGER,
    venue                 TEXT,
    authors_json          TEXT NOT NULL DEFAULT '[]',
    cited_by_count        INTEGER,
    referenced_works_json TEXT NOT NULL DEFAULT '[]',
    pdf_path              TEXT,
    pdf_sha256            TEXT,
    discovery_source      TEXT,
    first_seen_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_indexed_at       TEXT,
    retracted_at          TEXT,                                      -- Stage 2
    h_index               REAL,                                      -- Stage 2
    q_score               REAL,                                      -- Stage 2
    abstract_translated   TEXT,                                      -- Stage 3
    institution           TEXT,                                      -- Stage 3
    profile               TEXT,                                      -- v2: 'academic'|'dev'|'docs'|'startup'|...
    source_type           TEXT,                                      -- v2: 'paper'|'repo'|'qa'|'doc'|'forum'|'filing'|'page'
    quality_score         REAL,                                      -- v2: 0-1, profile-specific (distinct from academic q_score)
    url                   TEXT,                                      -- v2: canonical URL for non-DOI sources
    external_id           TEXT,                                      -- v2: e.g. github:owner/repo, so:question:12345
    CHECK (paper_id <> ''),
    CHECK (title    <> ''),
    -- discovery_source was previously CHECK-constrained to academic sources; v2 uses source_type for non-academic (ADR-006)
    CHECK (year IS NULL OR (year BETWEEN 1800 AND 2100))
);

CREATE UNIQUE INDEX IF NOT EXISTS papers_doi_uniq
    ON papers (doi) WHERE doi IS NOT NULL;
CREATE INDEX IF NOT EXISTS papers_arxiv_idx
    ON papers (arxiv_id) WHERE arxiv_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS papers_openalex_idx
    ON papers (openalex_id) WHERE openalex_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS papers_s2_idx
    ON papers (s2_paper_id) WHERE s2_paper_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS papers_title_norm_idx
    ON papers (title_norm);
CREATE INDEX IF NOT EXISTS papers_year_idx
    ON papers (year DESC) WHERE year IS NOT NULL;
CREATE INDEX IF NOT EXISTS papers_first_seen_idx
    ON papers (first_seen_at);

-- Stage 2 indexes ----------------------------------------------------
-- Partial so the index only stores retracted/scored rows (cheap on the
-- 99%+ of papers that are neither retracted nor yet q-scored).
CREATE INDEX IF NOT EXISTS papers_retracted_idx
    ON papers (retracted_at) WHERE retracted_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS papers_qscore_idx
    ON papers (q_score DESC) WHERE q_score IS NOT NULL;

-- v2 indexes (papers_profile_idx, papers_external_id_idx) are created
-- post-ALTER from index.py::_migrate_schema, since SQLite cannot create
-- a partial index that references a column the table doesn't yet have on
-- legacy 1.0/1.1/1.2 corpus.db files.

-- ============================================================
-- chunks — paper full-text split into ~500-token windows
-- ============================================================
-- INSERT discipline: index.py uses `INSERT OR REPLACE` keyed on
-- (paper_id, seq) so re-running the indexer is idempotent. The
-- UNIQUE constraint below enforces that contract at the DB level.
-- ============================================================
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id     TEXT    NOT NULL,
    seq          INTEGER NOT NULL,
    text         TEXT    NOT NULL,
    n_tokens     INTEGER NOT NULL,
    model_name   TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (paper_id) REFERENCES papers(paper_id) ON DELETE CASCADE,
    UNIQUE (paper_id, seq),
    CHECK (n_tokens > 0),
    CHECK (seq >= 0)
);

CREATE INDEX IF NOT EXISTS chunks_paper_idx
    ON chunks (paper_id);
CREATE INDEX IF NOT EXISTS chunks_model_idx
    ON chunks (model_name);

-- ============================================================
-- vec_chunks — sqlite-vec virtual table (vec0) holding embeddings
-- ============================================================
-- INVARIANT: vec_chunks.rowid == chunks.chunk_id.
--   - upsert_chunks() in index.py MUST insert into chunks first, read back
--     the AUTOINCREMENT chunk_id, and then insert into vec_chunks with that
--     exact rowid. The vec0 vtab silently allows rowid assignment via
--     `INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)`.
--   - Retrieval is `JOIN vec_chunks v ON v.rowid = c.chunk_id` so any drift
--     breaks search correctness, not just performance.
--   - On DELETE FROM chunks (CASCADE from papers), index.py MUST also issue
--     `DELETE FROM vec_chunks WHERE rowid IN (...)` — vec0 has no FK support.
--
-- 768 dims matches sentence-transformers/allenai-specter output (ADR-004).
-- Default metric for sqlite-vec MATCH on float[] is L2; query-time cosine
-- is computed via `vec_distance_cosine()` when needed.
-- ============================================================
CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
    embedding float[768]
);

-- ============================================================
-- citations — Stage 2 traversal target (now actively populated)
-- ============================================================
-- Stage 1 left this empty. Stage 2 fills it from S2 / Crossref / OpenAlex
-- reference lists. Schema is unchanged from Stage 1 so no migration needed.
-- ============================================================
CREATE TABLE IF NOT EXISTS citations (
    citation_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id       TEXT    NOT NULL,
    cited_doi      TEXT,
    cited_paper_id TEXT,
    direction      TEXT    NOT NULL,
    source_api     TEXT    NOT NULL,
    discovered_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (paper_id)       REFERENCES papers(paper_id) ON DELETE CASCADE,
    FOREIGN KEY (cited_paper_id) REFERENCES papers(paper_id) ON DELETE SET NULL,
    UNIQUE (paper_id, cited_doi, direction),
    CHECK (direction IN ('forward', 'backward')),
    CHECK (source_api IN ('s2', 'crossref', 'openalex')),
    CHECK (cited_doi IS NOT NULL OR cited_paper_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS citations_paper_dir_idx
    ON citations (paper_id, direction);
CREATE INDEX IF NOT EXISTS citations_cited_doi_idx
    ON citations (cited_doi) WHERE cited_doi IS NOT NULL;
CREATE INDEX IF NOT EXISTS citations_cited_paper_idx
    ON citations (cited_paper_id) WHERE cited_paper_id IS NOT NULL;

-- ============================================================
-- retractions — Stage 2 (Retraction Watch CSV ingest target)
-- ============================================================
CREATE TABLE IF NOT EXISTS retractions (
    retraction_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    doi             TEXT    NOT NULL,
    retraction_date TEXT,
    reason          TEXT,
    raw_record_json TEXT,
    ingested_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (doi),
    CHECK (doi <> '')
);

-- The UNIQUE(doi) above already builds an implicit index; we add an
-- explicit one only if Stage 2 profiling shows a hot non-unique scan.
-- (Skipped now to keep the index footprint minimal.)

-- ============================================================
-- rcs_scores — Stage 3 third-stage RCS LLM scorer output (PaperQA2 0-10)
-- ============================================================
-- One row per (chunk_id, query) pair. The scorer is expensive (LLM call
-- per chunk), so we key on query_hash = sha256(normalised_query)[:16] to
-- cache across runs. Per-query top-N retrieval reads
--   ORDER BY score DESC LIMIT N
-- under a fixed query_hash, which the covering index below makes
-- index-only (no chunks/papers lookup until the caller asks for them).
--
-- Both FKs CASCADE so re-indexing a paper (which deletes its chunks)
-- transparently invalidates the stale RCS rows.
-- ============================================================
CREATE TABLE IF NOT EXISTS rcs_scores (
    rcs_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id    TEXT    NOT NULL,
    chunk_id    INTEGER NOT NULL,
    query_hash  TEXT    NOT NULL,
    score       REAL    NOT NULL,
    summary     TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (paper_id) REFERENCES papers(paper_id) ON DELETE CASCADE,
    FOREIGN KEY (chunk_id) REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    UNIQUE (chunk_id, query_hash),
    CHECK (score >= 0.0 AND score <= 10.0),
    CHECK (query_hash <> '')
);

-- Covering composite: (query_hash, score DESC) lets the top-N-per-query
-- read be index-only when the caller only needs (chunk_id, score) — the
-- chunk_id is the rowid the index already carries.
CREATE INDEX IF NOT EXISTS rcs_scores_query_score_idx
    ON rcs_scores (query_hash, score DESC);

-- ============================================================
-- schema_meta — single-row migration marker / feature flags
-- ============================================================
CREATE TABLE IF NOT EXISTS schema_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Fresh installs land directly on 1.3. Existing 1.0/1.1/1.2 corpus.db files are
-- bumped to 1.3 by index.py::_ensure_schema() after the ALTER TABLE pass.
INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '1.3');
INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('stage_min',      '1');
INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('embedding_dim',  '768');
INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('embedding_model','sentence-transformers/allenai-specter');

-- ============================================================
-- Index rationale (one line each — full EQP report in dba handoff)
-- ============================================================
--   papers_doi_uniq             unique DOI lookup; partial so NULL DOIs don't collide
--   papers_arxiv_idx            partial; arXiv-only fallback path during dedup
--   papers_openalex_idx         partial; Stage 2 citation traversal joins on this
--   papers_s2_idx               partial; S2 enrichment cross-walk
--   papers_title_norm_idx       non-unique; title-collision dedup probe (Python decides)
--   papers_year_idx             partial DESC; supports "recent papers" ORDER BY year DESC
--   papers_first_seen_idx       incremental sync ("what landed today")
--   papers_retracted_idx        Stage 2 partial; scans only retracted rows (rare)
--   papers_qscore_idx           Stage 2 partial DESC; backs "top-N by quality" rankings
--   papers_profile_idx          v2 partial; profile-scoped scans ('dev', 'docs', ...)
--   papers_external_id_idx      v2 partial; lookup by github:/so:/etc. composite id
--   chunks_paper_idx            FK side; backs ON DELETE CASCADE + per-paper rebuilds
--   chunks_model_idx            lets us migrate one embedding model at a time
--   chunks (paper_id, seq)      implicit UNIQUE — idempotent re-index key
--   vec_chunks (vec0 vtab)      ANN search via MATCH; rowid joins chunks 1:1
--   citations_paper_dir_idx     composite (paper_id, direction) — forward/backward fetch
--   citations_cited_doi_idx     partial; "who cites DOI X" reverse lookup
--   citations_cited_paper_idx   partial; in-corpus citation graph queries
--   retractions UNIQUE(doi)     implicit; DOI is the only query key for retractions
--   rcs_scores_query_score_idx  Stage 3 covering (query_hash, score DESC); top-N per query
--   rcs_scores UNIQUE(chunk_id,query_hash)  implicit; (re)score upsert key + idempotent cache
-- ============================================================
