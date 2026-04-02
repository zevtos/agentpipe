# Building an AI-powered database engineering agent

**An AI database engineering agent is now feasible to build using MCP as the integration protocol, Atlas or Alembic for schema management, HypoPG/Dexter for index optimization, and a tiered safety model that defaults to read-only access.** The ecosystem matured rapidly through 2025: Google's MCP Toolbox supports 42+ data sources, Postgres MCP Pro offers workload-level index tuning, and the Supabase MCP server implements a four-tier risk classification that has become a reference architecture. This report covers every layer of the stack — from schema normalization rules an agent can apply programmatically to backup automation with pgBackRest and Litestream — with specific tools, SQL patterns, and architectural decisions for each.

---

## Schema design automation starts with declarative tools

An AI agent designing schemas needs two capabilities: generating correct DDL from requirements and detecting normalization violations in existing schemas. Both are now well-supported by tooling.

**Automated schema generation** follows a repeatable pattern. The agent extracts entities and relationships from natural language or ER models, infers candidate keys (suggesting surrogate keys like `SERIAL`/`BIGSERIAL` when natural keys are composite), maps relationships to FK structures (1:N → FK on the "many" side, M:N → junction table), selects data types by domain (emails → `VARCHAR(255) UNIQUE`, monetary → `NUMERIC(p,s)`, timestamps → `TIMESTAMPTZ`), and generates constraints (NOT NULL, CHECK, DEFAULT). Tools like Workik AI and AI2SQL already do this from natural language prompts.

**Normalization violation detection** can be implemented programmatically through data profiling:

- **1NF violations**: Scan for columns containing comma-delimited lists, JSON arrays in non-JSON columns, or repeating column groups (`phone1`, `phone2`, `phone3`)
- **2NF violations**: For tables with composite PKs, test whether any non-key column depends on only part of the PK via `SELECT COUNT(DISTINCT col) FROM t GROUP BY pk_part1`
- **3NF violations**: Profile functional dependencies between non-key columns: `SELECT CASE WHEN COUNT(DISTINCT b) = 1 THEN 'FD: a→b' END FROM t GROUP BY a`
- **BCNF**: Verify that for every functional dependency X→Y, X is a superkey — stricter than 3NF when multiple overlapping candidate keys exist

**Denormalization strategies** the agent should recommend when performance demands it include materialized views (PostgreSQL's `CREATE MATERIALIZED VIEW` with `REFRESH CONCURRENTLY`), CQRS with separate normalized write and denormalized read models, computed columns stored via triggers, and JSONB/JSON columns for semi-structured data alongside relational data.

### Migration tools and how agents should use them

**Atlas** (by Ariga) is the strongest choice for AI agent integration. Its declarative, schema-as-code approach eliminates manual migration planning — developers define desired state in HCL, SQL, or via ORM schema loaders (supporting **16 ORMs** including SQLAlchemy, GORM, Django, Prisma, and TypeORM), and Atlas automatically computes migration plans. Key commands: `atlas schema inspect` outputs HCL/JSON/SQL/ERD, `atlas schema diff` compares any two schema states, `atlas migrate lint` runs **50+ built-in static analyzers**, and `atlas schema apply` applies the desired state. Atlas supports PostgreSQL, MySQL, SQLite, ClickHouse, SQL Server, Oracle, Snowflake, CockroachDB, and more. Its Drift Inspector monitors live databases and sends Slack alerts when schemas diverge from their intended state. In 2025, Atlas released Agent Skills — an open standard for packaging migration expertise for AI coding assistants.

**Alembic** (Python/SQLAlchemy) is the best choice for Python-based agents. Its `autogenerate` feature compares SQLAlchemy MetaData against a live database and produces migration scripts. The programmatic API — `alembic.autogenerate.produce_migrations(context, metadata)` — returns a `MigrationScript` structure an agent can inspect and modify. The `alembic check` command returns an error code if pending changes exist, useful in CI pipelines.

Other migration tools with their agent-integration profiles:

| Tool | Approach | Auto-planning | Schema diff | Best for |
|------|----------|--------------|-------------|----------|
| **Atlas** | Declarative | Yes | Yes (built-in) | Multi-DBMS, AI agents |
| **Alembic** | Versioned + autogenerate | Yes | Yes | Python ecosystems |
| **Prisma** | Declarative | Yes | Limited | TypeScript/Node.js |
| **Flyway** | Versioned SQL | No | Enterprise only | Java, simple SQL workflows |
| **Liquibase** | Versioned changelogs | No | Yes (`diff-changelog`) | Enterprise, XML/YAML workflows |
| **SchemaHero** | Declarative K8s CRDs | Yes | Implicit | Kubernetes/GitOps |
| **golang-migrate** | Versioned up/down SQL | No | No | Go, simple CLI scripting |

For **schema diffing**, Atlas's `atlas schema diff` is the most comprehensive cross-DBMS tool. Stripe's open-source **pg-schema-diff** (Go) computes PostgreSQL diffs with a hazard system that warns about table locks and downtime. **Bytebase** provides an open-source database DevSecOps platform with schema review and drift detection.

### Multi-DBMS type abstraction

SQLAlchemy's three-tier type system is the gold standard for cross-database abstraction: generic "CamelCase" types (`Integer`, `String`, `DateTime`, `JSON`) auto-map to dialect-specific types, while `with_variant()` composes cross-dialect types. Key type mappings agents must handle:

| Concept | PostgreSQL | MySQL | SQLite | MongoDB | ClickHouse |
|---------|-----------|-------|--------|---------|------------|
| JSON document | `JSONB` (indexable) | `JSON` (text-based) | TEXT | Native document | `String` |
| UUID | `UUID` native | `CHAR(36)` / `BINARY(16)` | TEXT | String | `UUID` |
| Boolean | `BOOLEAN` | `TINYINT(1)` | `INTEGER` | Boolean | `UInt8` |
| Auto-increment PK | `BIGSERIAL` | `AUTO_INCREMENT` | `INTEGER PRIMARY KEY` | `ObjectId` | Manual |
| Array | `ARRAY` native | JSON array | N/A | Native array | `Array(T)` |

---

## Index selection and query optimization across databases

### Choosing the right index type

PostgreSQL offers **seven index types**, and an agent must select correctly based on workload:

**B-tree** handles 90%+ of workloads — equality, range, sorting, and `LIKE 'prefix%'`. **GIN** (Generalized Inverted Index) excels at multi-valued data: JSONB queries with `@>`, array containment with `&&`, and full-text search with `@@`. Create with `CREATE INDEX idx ON docs USING GIN (data jsonb_path_ops);` for optimal JSONB performance. **BRIN** (Block Range Index) provides 100–1000x size reduction for huge append-only tables where physical row order correlates with column values — a timestamp BRIN index on a 500M-row table can be a few hundred KB vs. gigabytes for B-tree. **GiST** supports geometric and range types; combined with the `btree_gist` extension, it powers exclusion constraints. **PostgreSQL 18** (September 2025) introduced skip scan on multicolumn B-tree indexes and parallel GIN builds.

For MySQL, the **leftmost prefix rule** governs composite index usage: a `(col1, col2, col3)` index supports lookups on `(col1)`, `(col1, col2)`, and `(col1, col2, col3)` but not `(col2)` alone. MySQL 8.0+ supports functional indexes on expressions. MongoDB follows the **ESR rule** (Equality → Sort → Range) for compound index column ordering, and only one array field per compound index is allowed.

**Covering indexes** eliminate table lookups entirely. PostgreSQL 11+ supports `INCLUDE` clauses: `CREATE INDEX idx ON orders (customer_id) INCLUDE (total_amount, order_date);`. MySQL indicates covering index use with `Using index` in EXPLAIN's Extra column. MongoDB requires projecting only indexed fields with `_id: 0` to achieve covered queries.

**Partial indexes** are a powerful optimization: `CREATE INDEX idx_orders_active ON orders (created_at) WHERE status != 'completed';` can reduce index size by 95% if most orders are completed.

### Automated index recommendation pipeline

An agent should implement this programmatic pipeline:

1. **Identify slow queries** via `pg_stat_statements` (sorted by `mean_exec_time` or `total_exec_time` for queries with `calls > 1000`)
2. **Analyze predicates** using **pg_qualstats**, which tracks WHERE/JOIN clause statistics and provides `pg_qualstats_index_advisor()` for global index suggestions
3. **Test hypothetical indexes** with **HypoPG** — `SELECT * FROM hypopg_create_index('CREATE INDEX ON orders(customer_id)');` creates an in-memory-only index, then `EXPLAIN (FORMAT JSON)` verifies the optimizer would use it
4. **Batch recommendations** via **Dexter** (`dexter --pg-stat-statements`), which internally uses HypoPG to validate every suggestion and only recommends indexes that improve query plans
5. **Apply safely** with `CREATE INDEX CONCURRENTLY` (non-blocking in PostgreSQL)

For MySQL, the `performance_schema.table_io_waits_summary_by_index_usage` view identifies unused indexes (where `rows_selected = 0`), and Percona Toolkit's `pt-index-usage` analyzes slow query logs.

### Parsing EXPLAIN output programmatically

The agent should always request JSON-formatted execution plans for structured parsing:

```sql
-- PostgreSQL (maximum information):
EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, FORMAT JSON) SELECT ...;

-- MySQL:
EXPLAIN FORMAT=JSON SELECT ...;
```

Key anti-patterns the agent should detect from parsed plans:

| Anti-pattern | PostgreSQL signal | MySQL signal |
|-------------|------------------|--------------|
| Full table scan on large table | `Seq Scan` with high row count | `type: ALL` |
| Disk-based sort | `Sort Method: external merge Disk` | `Extra: Using filesort` |
| Inaccurate estimates | `Actual Rows` >> `Plan Rows` | `rows` >> actual |
| Nested loop on large sets | `Nested Loop` with high loops | `type: ALL` in inner table |
| Unused available index | `Seq Scan` despite existing index | `key: NULL` with `possible_keys` |

The **auto_explain** extension automatically logs plans for queries exceeding a threshold — configure with `auto_explain.log_min_duration = 100` (milliseconds), `auto_explain.log_format = JSON`, and `auto_explain.sample_rate = 0.1` for 10% sampling to limit overhead to ~2%.

### Query rewriting patterns the agent should apply

The most impactful automated rewrites:

**Non-sargable to sargable** (enables index use): Transform `WHERE YEAR(signup_date) = 2024` into `WHERE signup_date >= '2024-01-01' AND signup_date < '2025-01-01'`. **Correlated subquery to JOIN**: Replace scalar subqueries in SELECT lists with explicit JOINs. **IN subquery to EXISTS or JOIN**: `WHERE customer_id IN (SELECT id FROM ...)` often performs better as `WHERE EXISTS (SELECT 1 FROM ... WHERE ...)`. **Pre-aggregate before JOIN**: When joining large tables for aggregation, aggregate first in a subquery to reduce join cardinality. **SQLFluff** provides automated SQL linting and fixing across 30+ dialects, with rules like ST05 that convert subqueries to CTEs automatically.

---

## Constraints, integrity, and safe constraint migrations

### Cross-DBMS constraint differences matter

**Foreign keys** behave differently across databases in ways an agent must handle. PostgreSQL supports `DEFERRABLE INITIALLY DEFERRED` constraints (checked at transaction commit, not statement execution) — MySQL does not. SQLite **disables foreign key enforcement by default**; the agent must issue `PRAGMA foreign_keys = ON` per connection. PostgreSQL 15+ supports partial SET NULL: `ON DELETE SET NULL (author_id)` targeting specific columns.

**CHECK constraints** in MySQL were silently ignored before version 8.0.16 (April 2019). Post-8.0.16, they're fully enforced but cannot contain non-deterministic functions, subqueries, or stored procedures. PostgreSQL CHECK constraints can reference user-defined functions.

**Exclusion constraints** (PostgreSQL-only) prevent overlapping ranges — essential for booking systems:

```sql
CREATE EXTENSION btree_gist;
CREATE TABLE reservations (
  room_id INTEGER,
  booking TSRANGE,
  EXCLUDE USING GIST (room_id WITH =, booking WITH &&)
);
```

**PostgreSQL domain types** enable reusable validation that an agent can apply across tables: `CREATE DOMAIN email AS TEXT CHECK (VALUE ~* '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]+$');` — then use `email` as a column type anywhere.

For **MongoDB**, schema validation uses `$jsonSchema` with `validationLevel` (strict/moderate/off) and `validationAction` (error/warn). The agent can enforce types, required fields, regex patterns, and numeric ranges.

### Adding constraints safely to production tables

The agent should always use PostgreSQL's **NOT VALID + VALIDATE pattern** for zero-downtime constraint additions:

```sql
-- Step 1: Add constraint without scanning existing data (instant, AccessExclusive lock briefly)
ALTER TABLE orders ADD CONSTRAINT fk_orders_customer
  FOREIGN KEY (customer_id) REFERENCES customers(id) NOT VALID;

-- Step 2: Validate existing rows (ShareUpdateExclusiveLock — reads and writes continue)
ALTER TABLE orders VALIDATE CONSTRAINT fk_orders_customer;
```

This two-step approach avoids long-held exclusive locks on large tables. The same pattern works for CHECK constraints and, in PostgreSQL 12+, for NOT NULL (add as `CHECK (col IS NOT NULL) NOT VALID`, validate, then `ALTER COLUMN SET NOT NULL`).

**Constraint naming conventions** the agent should enforce: `fk_<child>_<parent>_<column>` for foreign keys, `chk_<table>_<description>` for checks, `uq_<table>_<columns>` for uniques, `pk_<table>` for primary keys.

### Referential integrity in distributed systems

Cross-shard foreign keys remain a fundamental challenge. **Vitess 18+** supports FK enforcement within the same shard — tables related by FKs must share a common Vindex (sharding key) to co-locate. Cross-shard FKs use best-effort transactions that can leave inconsistent state on partial failure. **CockroachDB** provides full distributed FK support with ACID guarantees, automatically creating secondary indexes on FK columns. For systems without native distributed FK support, the agent should recommend application-level patterns: API validation before inserts, saga patterns with compensating transactions, or eventual consistency with background reconciliation jobs.

---

## Backup and recovery across every DBMS

### PostgreSQL: pgBackRest is the production standard

**pgBackRest** provides parallel backup/restore, encryption, multi-repository support (S3/Azure/GCS), block-level incremental backups, and a `verify` command for integrity checking. Configuration:

```ini
# pgbackrest.conf
[global]
repo1-path=/var/lib/pgbackrest
repo1-retention-full=2
repo1-retention-diff=7
compress-type=zst

# postgresql.conf
archive_mode = on
archive_command = 'pgbackrest --stanza=mydb archive-push %p'
```

A typical schedule: full backup Sunday 2 AM, differential daily Monday–Saturday. **PITR** restores to any point with `pgbackrest --type=time --target="2025-03-05 10:44:00+00" restore`. **WAL-G** is the cloud-native alternative, optimized for S3/GCS/Azure with delta backups and WAL integrity verification.

For **pg_dump**, the agent should prefer directory format (`-Fd`) with parallel workers (`-j 8`) for large databases. Custom format (`-Fc`) supports selective restore by table. Always dump globals separately: `pg_dumpall --globals-only > globals.sql`.

### MySQL: Percona XtraBackup for physical, MySQL Shell for logical

On a **96GB benchmark**, Percona XtraBackup was fastest overall for both backup and restore. MySQL Shell's `dump-instance` provides the best parallel logical backup with good compression. The agent should use XtraBackup for production hot backups and MySQL Shell for cross-version portability. Binlog-based PITR uses `mysqlbinlog --start-datetime --stop-datetime` piped to the mysql client.

### SQLite: Litestream for continuous replication

**Litestream** continuously streams WAL changes to S3/Azure/SFTP, achieving near-real-time RPO (seconds). It requires WAL mode (`PRAGMA journal_mode=WAL`) and takes over checkpointing. For simpler needs, `VACUUM INTO '/path/to/backup.db'` creates a consistent snapshot from a single transaction. The SQLite Online Backup API enables incremental page-by-page copying while the source remains readable.

### Disaster recovery planning

The agent should help teams define explicit **RPO/RTO targets**: WAL archiving achieves seconds of RPO; hourly pg_dump allows up to 1 hour; daily snapshots allow up to 24 hours. RTO depends on backup type — physical restores (pgBackRest/XtraBackup) take minutes to low hours, while logical restores (pg_restore) scale with database size. AWS RDS Multi-AZ failover achieves **1–2 minute RTO**. Backup verification through automated restore tests, checksum verification, and post-restore row count comparisons is essential — the agent should schedule and monitor these.

---

## MCP is the integration protocol for database agents

### The MCP ecosystem for databases has matured rapidly

**Model Context Protocol** (MCP), introduced by Anthropic in November 2024 and adopted by OpenAI in March 2025, uses a client-server architecture with JSON-RPC messaging. Servers expose tools (executable functions), resources (data), and prompts (templates). Implement once, work across Claude Desktop, Cursor, VS Code, Windsurf, and Claude Code.

The leading MCP database servers as of early 2026:

**Google MCP Toolbox for Databases** (googleapis/genai-toolbox) is the most production-ready general solution, supporting **42+ data sources** with connection pooling, OAuth2 authentication, and OpenTelemetry observability. Tools are defined declaratively in `tools.yaml`, with SDKs for Python, JavaScript, TypeScript, Go, and Java.

**Postgres MCP Pro** (crystaldba/postgres-mcp, 2K+ GitHub stars) is the most feature-rich PostgreSQL-specific server, offering `get_explain` for execution plan analysis, `recommend_indexes` using industrial-strength algorithms with hypothetical index simulation, `get_slow_queries` from pg_stat_statements, and `check_health` for comprehensive database health assessment (buffer cache, connections, vacuum health, replication lag, sequence limits).

**DBHub** (bytebase/dbhub, 2K+ stars, 100K+ downloads) takes a minimalist approach with just two core tools — `execute_sql` and `search_objects` (progressive schema disclosure) — maximizing token efficiency. Supports PostgreSQL, MySQL, MariaDB, SQL Server, and SQLite.

**Supabase MCP Server** implements the most sophisticated safety model with four risk tiers: low (SELECT), medium (INSERT/UPDATE/DELETE), high (DROP/TRUNCATE, blocked even in unsafe mode), and extreme (blocked entirely). It uses `pglast` — PostgreSQL's actual parser — for runtime SQL validation and auto-versions all schema changes.

### What tools should a database agent expose?

Based on analysis of production MCP servers, the essential tool categories are:

- **Schema intelligence**: `list_schemas`, `list_tables`, `describe_table`, `get_object_details` — providing context for correct SQL generation
- **Query execution**: Separate `query` (read-only) and `execute` (read-write) tools with parameter support and configurable timeouts
- **Performance analysis**: `get_explain` with hypothetical index support, `get_slow_queries` from pg_stat_statements
- **Index tuning**: `recommend_indexes` (workload-level) and `analyze_workload_indexes` (query-specific)
- **Health monitoring**: `check_health` covering buffer cache hit rates, connection utilization, vacuum health, replication lag, unused/duplicate indexes
- **Migration management**: Schema diff, migration generation, lint analysis, and apply with approval workflows

---

## Safety guardrails are non-negotiable

### Defense in depth with five layers

Every production database agent implements multiple safety layers. The recommended architecture combines:

**Layer 1 — Database-level permissions.** Create a dedicated agent role with minimal privileges. Default to `SELECT`-only on production. Use PostgreSQL Row Level Security (RLS) to enforce row-level access policies at the database level, independent of the application.

**Layer 2 — SQL parsing and classification.** Use actual database parsers (`pglast` for PostgreSQL) to classify every query by risk level before execution. The Supabase four-tier model (low/medium/high/extreme) is the best reference architecture. Block `DROP DATABASE`, `TRUNCATE`, and other catastrophic operations at the parser level, not with regex.

**Layer 3 — Query validation.** **sql-data-guard** (ThalesGroup, open-source) was built specifically for the LLM-database gap where traditional parameterized queries don't apply. It enforces column allowlisting, detects always-true expressions, and automatically appends restriction clauses (e.g., `WHERE account_id = 123`).

**Layer 4 — Human-in-the-loop.** Scope approval gates to specific tool invocations rather than full agent outputs. Low-risk reads proceed autonomously; structural changes (ALTER, CREATE INDEX, migrations) require explicit human confirmation. Persist state at approval checkpoints to allow resumption without replaying prior work.

**Layer 5 — Audit and observability.** Log every prompt, query, and result. Google MCP Toolbox's OpenTelemetry integration provides the trace/metric foundation. Vanna 2.0 tracks every query per user for compliance. As Cisco's security guidance states: "When controls fail, your ability to investigate depends on having a complete record."

A novel approach worth noting: **Exoagent's object-capability model** makes dangerous queries structurally impossible to construct. The agent writes JavaScript in a sandbox and accesses the database only through typed query builder objects that enforce data boundaries at the AST level. In a CTF challenge, system-prompt-based protections were broken in minutes; the capability-protected agent remained unbroken.

### Environment separation is critical

**Never connect an AI agent directly to a production database.** Use read replicas for analysis, staging environments for testing changes, and named instances (pgEdge's pattern: `devdb`, `stagingdb`, `proddb`) with different permission levels. Dry-run operations via `EXPLAIN` without execution, and test hypothetical indexes with HypoPG before creating real ones.

---

## Real-world AI database tools already in production

**D-Bot / DB-GPT** (Tsinghua University, VLDB 2024 / SIGMOD 2025) is the most advanced research system — an LLM-based diagnosis system that extracts knowledge from database documentation, uses tree-of-thought reasoning for root cause analysis, and generates diagnosis reports within 10 minutes versus hours for a human DBA. Multiple LLMs collaborate on different diagnostic areas.

**Vanna.ai** (20K+ GitHub stars) evolved from a text-to-SQL library to a production agent framework. Vanna 2.0 uses RAG — training on DDL, documentation, and query examples stored in a vector database — with user-aware components, row-level security, and lifecycle hooks for quota checking and audit logging. It supports any LLM and any database.

**Wren AI** (10K+ GitHub stars) takes a semantic-first approach with a Modeling Definition Language that maps business terms to data structures. Its Wren Engine, built on Apache DataFusion (Rust), is now MCP-compatible.

**DBtune** (Stanford/Lund University research) is an open-source Go agent for PostgreSQL parameter tuning that has delivered up to **2.15x speedup** on benchmarks by tuning 13+ parameters. **Aiven's AI Database Optimizer** (powered by EverSQL, 120K+ users, 2M+ queries optimized) delivers non-intrusive index recommendations and query rewrites for PostgreSQL and MySQL.

---

## Recommended architecture for the agent

The optimal architecture for a world-class database engineering agent combines these components into a coherent system:

**Protocol layer**: MCP servers per database instance, exposing schema inspection, query execution, EXPLAIN analysis, index recommendation, health monitoring, and migration management tools. Use Google MCP Toolbox for multi-database support or Postgres MCP Pro for PostgreSQL-specific depth.

**Schema management**: Atlas for declarative, multi-DBMS schema-as-code with automatic migration planning and 50+ lint analyzers. Integrate Atlas Agent Skills for direct AI coding assistant support. Use `atlas schema diff` for drift detection with Slack alerting.

**Performance optimization loop**: Continuous monitoring via pg_stat_statements / performance_schema → slow query identification → EXPLAIN analysis with JSON parsing → hypothetical index testing via HypoPG → recommendation generation with Dexter → `CREATE INDEX CONCURRENTLY` with human approval → regression monitoring via Prometheus exporters and Grafana dashboards.

**Backup orchestration**: pgBackRest for PostgreSQL (full weekly + differential daily + continuous WAL), Percona XtraBackup for MySQL, Litestream for SQLite. Automated restore testing via Kubernetes CronJobs. The agent monitors backup success, verifies integrity, and alerts on failures.

**Safety architecture**: Five-layer defense (database permissions → SQL parsing → query validation → human-in-the-loop → audit logging). Default read-only. Four-tier risk classification. Environment separation with named instances. Object-capability model for highest-security deployments.

## Conclusion

The tooling for AI-powered database management crossed a critical threshold in 2025. MCP eliminated the integration fragmentation problem — one protocol now connects agents to any database through any AI client. Atlas's declarative schema management and Agent Skills standard mean an agent no longer needs to manually craft migration SQL. HypoPG and Dexter enable safe, automated index optimization without touching production data. The key insight from production deployments is that **safety architecture matters more than capability**: the Supabase four-tier risk model, sql-data-guard's query validation, and Exoagent's object-capability approach demonstrate that robust guardrails enable rather than constrain agent autonomy. The most impactful starting point for building this agent is a read-only MCP server connected to pg_stat_statements and HypoPG — providing immediate value through performance analysis and index recommendations without any write risk.