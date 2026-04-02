# DevOps observability and operations: a complete agent instruction set

**OpenTelemetry, structured logging, SLO-based alerting, expand-contract migrations, and shift-left security scanning form the five pillars every autonomous DevOps agent must enforce across any stack.** This document provides concrete, enforceable rules — not vague guidelines — organized by priority. Each section includes decision logic, anti-patterns to forbid, and references to the industry specifications that underpin them. The instructions are stack-agnostic: they apply whether the service runs Python/FastAPI, Node.js, Go, Java, or anything else, on Kubernetes, Docker, bare metal, or cloud-managed infrastructure.

---

## 1. Logging and tracing: the foundation of all observability

### 1.1 Structured logging rules

Every log entry **must be a single-line JSON object** written to `stdout` (informational) or `stderr` (errors). This is non-negotiable across all stacks. Unstructured string logs, multi-line stack dumps outside JSON wrappers, and file-based logging inside containers are forbidden.

**Required fields in every log entry:**

| Field | Format | Source |
|-------|--------|--------|
| `timestamp` | ISO 8601 UTC with ms precision: `2025-03-15T14:30:00.123Z` | OTel Semantic Conventions |
| `level` | `TRACE`, `DEBUG`, `INFO`, `WARN`, `ERROR`, `FATAL` | Consistent casing across all services |
| `service.name` | String: `"billing-api"` | OTel resource attribute |
| `service.version` | Semver string | OTel resource attribute |
| `deployment.environment` | `production`, `staging`, `development` | OTel resource attribute |
| `trace_id` | 32-char hex (W3C format) | W3C Trace Context |
| `span_id` | 16-char hex | W3C Trace Context |
| `message` | Terse human-readable description | — |

Additional fields for error logs: `error.type` (exception class), `error.message`, `error.stack_trace` (as a single escaped string). Additional recommended fields: `request_id`, `http.method`, `http.status_code`, `http.route`, `duration_ms`, `k8s.pod.name`, `k8s.namespace.name`.

**Use OpenTelemetry Semantic Conventions** for all field naming. Elastic contributed the Elastic Common Schema (ECS) to OpenTelemetry in 2023; they are converging into one unified schema. Use `snake_case` with dot-separated namespaces (`http.method`, `db.system`). Put structured data in dedicated fields — never interpolate variables into message strings.

**Log level decision logic:** Production services default to `INFO`. Staging defaults to `DEBUG`. If DEBUG is needed in production, sample at **1%** rather than enabling globally. `TRACE` is always OFF in production. Use `WARN` for recoverable but unexpected conditions (deprecated API calls, retries that succeeded, approaching resource limits). Use `ERROR` only for operation failures requiring investigation — never for expected business conditions like input validation failures. Use `FATAL` only for unrecoverable states that cause process exit.

**Correlation and context propagation:** Generate a `request_id` (UUID v4) at the API gateway or entry point. Propagate via `X-Request-ID` header to all downstream services. Use language-native request-scoped context mechanisms: `AsyncLocalStorage` (Node.js), `MDC`/`ThreadLocal` (Java), `contextvars` (Python), `context.Context` (Go), `Serilog LogContext` (.NET). Set context once at request entry — every subsequent log in that request automatically includes it.

**Anti-patterns to forbid:** Logging the same error at multiple catch layers (log where you handle, not where you rethrow). `console.log(request.body)` in production — the number-one source of PII leaks. Logging every SQL query in production. Logging health check requests (`/health`, `/ready`, `/metrics`). Using `ERROR` level for user input validation failures.

### 1.2 What to log and what to never log

**Never log these items** (per OWASP Logging Cheat Sheet and GDPR/PCI-DSS requirements): passwords, API keys, tokens (JWT, OAuth, session), private keys, certificates, database connection strings, encryption keys, full payment card numbers, CVVs, PINs, SSNs, passport numbers, health/medical data (HIPAA PHI), full bank account numbers, source code, or internal network topology.

**Mask before logging:** Email addresses → `j***@example.com`. Phone numbers → last 4 digits only. IP addresses → hash or anonymize for GDPR. Usernames → use opaque user IDs. Request/response bodies → never log full bodies by default; whitelist specific safe fields.

**Critical rule: mask at the source.** PII redaction must happen in-process before any network call to log storage. Do not rely solely on downstream pipeline processors. If PII reaches your log database, it is already a GDPR liability. Implement redaction in a central logging library wrapper, enforce it in CI/CD review gates, and run automated nightly scans to detect PII that slipped through. Use the OTel Collector's **Redaction Processor** and **Transform Processor** as a defense-in-depth layer, not the primary filter.

**What IS useful to log:** Authentication events (success/failure, MFA, role changes), authorization failures, application errors with structured stack traces, external service calls (URL, status code, duration — not payloads), business-critical operations (orders, payments, account creation), system lifecycle events (startup, shutdown, config changes, deployments), and performance data (request duration, queue depth, cache hit ratio).

### 1.3 Distributed tracing with OpenTelemetry

**OpenTelemetry is the mandatory instrumentation standard.** It is the second-largest CNCF project behind Kubernetes, with **79% of organizations** using or evaluating it. As of 2025, all three core signals — traces, metrics, and logs — are **Stable/GA** across all major language SDKs. Profiling is in Alpha. Never use vendor-specific SDKs for new services.

**Always use W3C Trace Context** for propagation. The `traceparent` header format is `00-{32-char-trace-id}-{16-char-span-id}-{trace-flags}`. The `tracestate` header carries vendor-specific key-value pairs. Forbid mixing propagation formats — if legacy services use B3/Zipkin headers (`X-B3-TraceId`), migrate them to W3C Trace Context.

**Span naming must follow OTel Semantic Conventions:** HTTP spans use `http.method`, `http.route`, `http.status_code`. Database spans use `db.system`, `db.statement`, `db.name`. Messaging spans use `messaging.system`, `messaging.destination`. RPC spans use `rpc.system`, `rpc.service`, `rpc.method`. Keep span names **low-cardinality** — never include user IDs, request IDs, or dynamic values in span names. Use span attributes for high-cardinality data.

**Sampling decision logic:**

- **Head-based sampling** (decision at trace creation): Start here. Set **10% sample rate** for high-traffic production, **100% for dev/staging**. Use `ParentBasedSampler` wrapping `TraceIdRatioBasedSampler` to ensure child spans respect the parent's decision. Simple, predictable, no special infrastructure.
- **Tail-based sampling** (decision after trace completes): Use when you need 100% capture of errors, latency outliers, or business-critical transactions. Requires an OTel Collector gateway. Set `decision_wait` to max expected trace latency + grace period (typically 10–30s). Size `num_traces` as `expected_new_traces_per_sec × decision_wait × 2x`.
- **Combined strategy (recommended for scale):** Head sampling at 10–20% in the SDK to reduce pipeline volume, then tail sampling at the Collector gateway for intelligent filtering. Grafana Labs runs this pattern at **2M+ spans/second**.

**Tail sampling policies, in order:** (1) Always keep errors (`status_code: ERROR`). (2) Always keep slow traces (latency > SLO threshold). (3) Always keep critical-service traces. (4) Probabilistic sample the remainder at 5–10%. Always include a probabilistic baseline — sampling only errors distorts service maps and RED metrics.

**Auto-instrumentation first, manual second.** Auto-instrumentation captures HTTP, database, gRPC, and messaging operations with zero code changes. Available for all major languages (Java agent, Python/Node.js auto-instrumentation). Layer manual instrumentation for business-critical paths and domain-specific spans. **Critical:** Initialize the OTel SDK before importing any instrumented libraries — late initialization means missing spans.

### 1.4 The OpenTelemetry Collector

**Always deploy an OTel Collector** between applications and backends. Never send telemetry directly from applications to storage backends in production.

The Collector pipeline model is `Receivers → Processors → Exporters`, configured per signal type. Use the `otelcol-contrib` distribution unless you have specific reasons for a custom build.

**Mandatory processor ordering:** `memory_limiter` (always first) → `resource` → `filter` → `attributes` → `transform` → `sampler` → `batch` (always last).

**memory_limiter configuration:** Set `limit_mib` to **70–80% of container memory limit**. Set `spike_limit_mib` to 20–25% of `limit_mib`. Set `check_interval: 1s`. Set the `GOMEMLIMIT` environment variable to **80% of the container hard memory limit** on every Collector deployment.

**batch processor:** `send_batch_size: 8192` (default), `timeout: 200ms` (increase to 1–10s for lower throughput). This dramatically improves export efficiency.

**Deployment pattern decision logic:** Small deployments → single Collector or node-level agents. Growing systems → add a gateway layer. Production at scale → **Agent + Gateway hybrid**: DaemonSet agents on each node for local collection and K8s metadata enrichment, forwarding to a load-balanced gateway cluster for tail sampling, PII scrubbing, and export. For tail sampling, all spans for a given trace must route to the same Collector instance — use `loadbalancingexporter` with trace ID affinity.

**Generate metrics from spans before sampling** using the `spanmetrics` and `servicegraph` connectors in the Collector. This preserves accurate RED metrics even when 90%+ of traces are sampled away.

**Anti-patterns to forbid:** Placing `batch` processor before `tail_sampling` (separates spans of the same trace). Using the `transform` processor excessively (CPU-intensive at scale). Replacing the OTel Collector with generic data pipelines like Vector or Fluentd — this loses OTel-native processing capabilities.

### 1.5 Logging infrastructure and tooling selection

**Container logging architecture:** Applications write to stdout/stderr only. In Kubernetes, the kubelet captures these and writes them to `/var/log/pods/`. Deploy a **DaemonSet logging agent** (Fluent Bit, Vector, or OTel Collector) to collect, enrich with K8s metadata, and forward to centralized storage. Use the sidecar pattern only when the app cannot write to stdout, multi-tenant isolation is required, or you have >500 collection configurations per cluster.

**Docker production setting:** Switch to non-blocking log mode (`"mode": "non-blocking", "max-buffer-size": "4m"`). The Docker default is blocking — when the buffer fills, the application blocks. Accept potential log loss during extreme backpressure in exchange for application availability.

**Log retention policies:**

| Category | Hot retention | Total retention |
|----------|--------------|-----------------|
| Development | 3–7 days | 3–7 days |
| Staging | 2–3 weeks | 2–3 weeks |
| Production application logs | 30–90 days | 6–12 months |
| Security/audit logs | 90 days searchable | 1–7 years per compliance |
| PCI-DSS | 3 months actively searchable | 1 year minimum |
| HIPAA | — | 6 years |
| SOX | — | 7 years |

Automate retention lifecycle with ILM policies, S3 lifecycle rules, or equivalent. Different log categories get different retention periods.

**Tooling decision logic:** If you need deep full-text search and have existing ELK investment → **Elasticsearch/ELK**. If you are Kubernetes-first, already use Grafana/Prometheus, and are cost-sensitive → **Grafana Loki** (up to 90% cheaper storage than ELK via metadata-only indexing on object storage). If you want fastest time-to-value with a unified managed platform and have budget → **Datadog**. If you have enterprise security/SIEM requirements → **Splunk**. For tracing backends: **Grafana Tempo** for cheapest storage at scale in the Grafana ecosystem, **Jaeger** for self-hosted control with adaptive sampling, **Datadog APM** for unified SaaS.

**Critical principle:** OTel instrumentation code does not change when switching backends. Always instrument with OTel, then choose or swap backends freely.

### 1.6 Log schema standardization and alerting

**Enforce consistent JSON schemas across polyglot services** using four layers: (1) Publish a versioned "Log Schema Contract" document specifying required fields, naming, and types. (2) Provide per-language example configurations (Go `slog`, Java Logback+Logstash encoder, Python `structlog`, Node.js `pino`, .NET `Serilog`). (3) Use the OTel SDK as the unifying layer — it auto-attaches `service.name`, `trace_id`, `span_id`, and resource attributes. (4) Use the OTel Collector as pipeline-level enforcement: transform/rename non-conforming fields, drop logs missing required fields, add missing metadata. Add a CI linting step that validates log output against the JSON schema.

**Log-based alerting operates in three layers.** Layer 1: Pattern-based alerts for critical errors (e.g., database connection failure — 3 occurrences in 5 minutes → page). Layer 2: Threshold-based alerts on error rates (5xx rate exceeding 5% in 1 minute → warning; exceeding 10% → critical). Layer 3: Anomaly detection with adaptive baselines (alert when error rate exceeds mean + 3 standard deviations over a 7-day window AND exceeds a 1% minimum significance floor). Add a **10-minute post-deployment settling window** to suppress false positives from new code paths. Monitor the logging pipeline itself as critical infrastructure: alert when buffer utilization exceeds 80% for 5 minutes, when delivery error rate exceeds 1%, or when log ingestion rate drops more than 50% from baseline.

---

## 2. Health checks and monitoring: knowing what's alive and performing

### 2.1 Probe types and when to use each

**Startup probe:** Checks whether the application has finished starting. Runs first; liveness and readiness probes are disabled until it succeeds. Use when the app takes more than 10 seconds to start (Java apps, apps running migrations).

**Liveness probe:** Determines if the application is alive. If it fails, the container is killed and restarted. Should only detect **fatal, unrecoverable states** — deadlocks, infinite loops, unrecoverable memory corruption. Must be lightweight (return 200 from `/healthz/live` with no external calls).

**Readiness probe:** Determines if the container can accept traffic. If it fails, the pod is removed from the load balancer but not restarted. Should check external dependency connectivity (database, cache, downstream APIs) at `/healthz/ready`.

**Decision logic:** Does the app take >10s to start? → Add startup probe. Can the app deadlock or hang in an unrecoverable state? → Add liveness probe. Does the app depend on external services? → Add readiness probe. All production services → always configure at minimum a readiness probe.

**Recommended Kubernetes configuration:**

```yaml
startupProbe:
  httpGet: {path: /healthz, port: 8080}
  periodSeconds: 5
  failureThreshold: 60  # Up to 5 min for startup
  timeoutSeconds: 3
livenessProbe:
  httpGet: {path: /healthz/live, port: 8080}
  periodSeconds: 15
  failureThreshold: 3   # 45s before restart
  timeoutSeconds: 3
readinessProbe:
  httpGet: {path: /healthz/ready, port: 8080}
  periodSeconds: 5
  failureThreshold: 3
  timeoutSeconds: 3
```

**Critical anti-pattern to forbid: checking database connectivity in liveness probes.** If the database goes down, all pods restart simultaneously, causing cascading failure. Database checks belong in readiness probes only. Also forbid: using the same endpoint with identical logic for both liveness and readiness, and setting overly aggressive timeouts (`timeoutSeconds: 1, failureThreshold: 1`).

**Health check response format** should follow the IETF draft `draft-inadarei-api-health-check-06` with media type `application/health+json`. Required field: `status` with values `"pass"`, `"fail"`, or `"warn"`. Aggregation rule: if any critical dependency fails → overall `"fail"` (HTTP 503); if any non-critical dependency fails → `"warn"` (HTTP 200). Cache dependency check results for 5–10 seconds to prevent health check storms.

### 2.2 Metrics collection: RED, USE, and the Four Golden Signals

**For every request-driven service (APIs, microservices), instrument the RED method:**

- **Rate**: Requests per second → Counter `http_requests_total`, query with `rate(http_requests_total[5m])`
- **Errors**: Failed requests per second → `rate(http_requests_total{status=~"5.."}[5m])`
- **Duration**: Latency distribution → Histogram `http_request_duration_seconds`, query with `histogram_quantile(0.99, ...)`

**For infrastructure resources, apply the USE method:** Utilization (% in use), Saturation (queued work — run queue length, swap usage), Errors (disk errors, packet drops). Apply USE per-resource: each CPU, each disk, each NIC.

**Metric type decision logic:** Tracking totals (requests, errors, bytes) → **Counter** (must end in `_total`, query with `rate()`). Tracking current values (memory, queue depth, connections) → **Gauge**. Tracking distributions (latency, response size) → **Histogram** (preferred over Summary because it can be aggregated across instances). Define histogram buckets aligned to SLO thresholds.

**Prometheus naming rules:** Names must use `[a-zA-Z_:][a-zA-Z0-9_:]*`. Include a single-word app prefix: `myapp_http_requests_total`. Use base units in the name: `_seconds` not `_milliseconds`, `_bytes` not `_kilobytes`. Counters must end in `_total`. Colons are reserved for recording rules only. Never put label values in metric names — use `http_requests_total{method="POST"}` not `http_requests_post_total`.

**Label cardinality is the most dangerous pitfall in metrics.** Never use unbounded values as labels: user IDs, email addresses, UUIDs, request IDs, timestamps, full URLs, IP addresses. Aim for <10 values per label, absolute max ~100. Total cardinality per metric should stay under **1,000 time series**. If you need high-cardinality data, put it in logs/traces, not metrics.

### 2.3 SLI/SLO/SLA implementation

An **SLI** (Service Level Indicator) is a quantitative ratio: `good events / total events`. An **SLO** (Service Level Objective) is an internal target for SLI performance over a rolling window — e.g., "99.9% of requests succeed over 28 days." An **SLA** is a contractual commitment with consequences; SLAs must always be less strict than SLOs. The **error budget** is `1 - SLO target` — for a 99.9% monthly SLO, that is **43.2 minutes** of allowed downtime.

Track **2–3 SLIs per service maximum**. Use **28-day rolling windows** (captures the same number of weekends regardless of start day). Start with conservative SLOs based on current performance (99.0–99.5%), improve gradually. Tier SLOs by criticality: payment systems 99.9%, reporting 99.5%, analytics 99.0%.

**Error budget policy:** Budget >50% remaining → ship features freely. 25–50% → increased review, freeze risky deploys. <25% → reliability work prioritized over features. Budget exhausted → feature freeze until budget replenishes.

**Use multi-window multi-burn-rate alerting** (Google SRE Workbook, Chapter 5). This is the most recommended approach, reducing alert volume by approximately **85%** while improving detection.

| Severity | Burn rate | Long window | Short window | Action |
|----------|-----------|-------------|--------------|--------|
| Page | 14.4× | 1 hour | 5 minutes | Immediate page |
| Page | 6× | 6 hours | 30 minutes | Immediate page |
| Ticket | 3× | 1 day | 2 hours | Create ticket |
| Ticket | 1× | 3 days | 6 hours | Create ticket |

The long window ensures budget spend is significant; the short window ensures the alert resets quickly after the incident resolves. Pre-compute all burn-rate windows as Prometheus recording rules. Forbid using the `for:` duration clause for SLO alerting — if the metric flickers, the timer resets and 100% error spikes every 10 minutes may never trigger.

### 2.4 Alerting that doesn't cause fatigue

**Core principle: every alert must be actionable.** If an alert fires and the on-call engineer cannot take a specific action, the alert must not exist.

**Always alert on symptoms** (user-visible impact: high latency, error rate, unavailability). **Never page on causes alone** (high CPU, high memory, disk at 90%). CPU at 95% but latency is normal? No page needed. Latency doubled but CPU at 50%? Page immediately. Cause-based metrics belong on dashboards for investigation.

**Severity levels:** P0/SEV1 (Critical: complete service failure, phone call, <5 min response). P1/SEV2 (High: degraded service, push notification, <15 min response). P2/SEV3 (Medium: partial impact, team chat during business hours, <4h response). P3/SEV4 (Low: informational, ticketing system or dashboard, next business day).

**Every alert must link to a runbook** containing: what the alert means, what users are experiencing, specific investigation steps (PromQL queries, dashboard links), step-by-step remediation, escalation contacts, and a system architecture diagram. Target **fewer than 2 pages per on-call shift** (Google SRE recommendation). Conduct monthly alert audits — if an alert was consistently ignored, revise or delete it.

**Synthetic monitoring:** Probe from minimum 3 geographically diverse locations. Do not alert on single-region failure — require confirmation from at least 2 regions. Check critical HTTP endpoints every **30–60 seconds**, API health every 1–3 minutes, multi-step transactions every 3–5 minutes, SSL certificate expiry every 1–6 hours.

---

## 3. Database migrations: changing schemas without downtime

### 3.1 The expand-contract pattern is mandatory for breaking changes

Any migration that renames, removes, or changes the type of a column, or adds a NOT NULL constraint, **must use the expand-contract pattern**. Additive-only changes (adding a nullable column, adding a table, creating an index concurrently) can be applied directly.

**Phase 1 — Expand:** Add new structures without removing old ones. All additions must be backward-compatible (nullable columns, no NOT NULL without defaults). Old application code continues to function unaware of changes.

**Phase 2 — Migrate data:** Deploy code that writes to both old and new structures (dual-write). Run background backfill in small batches (**1,000–5,000 rows per batch**) with checkpointing and explicit sleeps between batches (100ms) to reduce replication lag. Use feature flags to control the read/write switch for instant rollback capability.

**Phase 3 — Contract:** After a confidence window of **24–48 hours minimum**, stop dual writes, remove old columns/indexes, clean up feature flags and migration code.

**Specific zero-downtime recipes:**

- **Adding a column:** Always add as NULLABLE. PostgreSQL 11+ supports instant `ADD COLUMN ... DEFAULT value`, but older versions and MySQL may rewrite the table. Backfill in batches, then add NOT NULL constraint with `NOT VALID`, then `VALIDATE CONSTRAINT` separately.
- **Renaming a column** (6 steps): Add new column → deploy dual-write → backfill → switch reads (with fallback) → stop writing to old → drop old after 24–48h. Never use `ALTER TABLE RENAME COLUMN` in production.
- **Removing a column** (4 steps): Stop writing → wait one deploy cycle → stop reading (exclude from ORM SELECT) → drop column. Never drop in one step.
- **Adding indexes:** PostgreSQL: always use `CREATE INDEX CONCURRENTLY` (must run outside a transaction, disable `statement_timeout`). MySQL 8.0+: use `ALGORITHM=INSTANT` or `ALGORITHM=INPLACE` where supported.

**Anti-patterns to forbid:** Running `ALTER TABLE` without `lock_timeout`. Creating indexes without CONCURRENTLY (PostgreSQL). Running `UPDATE` on an entire large table in a single transaction. Adding NOT NULL in one step (requires full table scan under exclusive lock). Adding foreign keys without `NOT VALID`. Running DDL inside the application request path. Grouping all schema changes into one giant migration script.

### 3.2 Lock safety is critical for production databases

In PostgreSQL, DDL statements require an ACCESS EXCLUSIVE lock. If a long-running query holds any lock on the table, the DDL queues behind it, and **all subsequent queries queue behind the DDL**. This cascading lock queue can bring down the entire application.

**Mandatory rules:**

1. **Always set `lock_timeout` on DDL statements:** `SET lock_timeout = '2s';` before any DDL. If the lock can't be acquired in 2 seconds, the statement fails instead of blocking everything.
2. **Implement retry with exponential backoff** when lock acquisition fails.
3. **Set `statement_timeout` as a safety net:** `SET statement_timeout = '60s';` — but disable it for CONCURRENTLY operations.
4. **Check for long-running queries before migrating** by querying `pg_stat_activity`.
5. **Monitor lock contention** during migrations by querying `pg_locks`.

**Online schema change tools decision logic:** For MySQL write-heavy + no foreign keys → **gh-ost**. For MySQL with foreign keys → **pt-online-schema-change**. For PostgreSQL needing automated expand-contract → **pgroll**. For MySQL 8.0+ changes supporting INSTANT DDL → use native DDL. If the table has >1M rows and needs DDL → use online schema change tools. If the table has >100K rows and needs data backfill → use batched operations.

### 3.3 Migration tooling, versioning, and CI/CD integration

**Tool selection decision logic:** Single DB + SQL expertise → **Flyway**. Multi-database + enterprise features → **Liquibase**. Python/SQLAlchemy → **Alembic**. Go → **golang-migrate** or **Atlas**. Node.js/TypeScript → **Prisma Migrate** or **Knex**. PostgreSQL + zero-downtime automation → **pgroll**. Schema-as-code with CI linting → **Atlas**.

**Use timestamp-based naming** for teams of 2+ developers (`20250914120000_add_email_to_users.sql`) to eliminate merge conflicts. Sequential numbering is acceptable only for solo developers. **Never modify an already-applied migration** — all tools store checksums and will reject tampered files. Create a new migration instead.

**Make all migrations idempotent** using `IF NOT EXISTS`/`IF EXISTS` guards, `CREATE OR REPLACE` for views/functions, and Liquibase `preconditions` blocks.

**Preferred CI/CD pipeline structure:** Build → Validate (dry-run against shadow database) → Test (apply to test DB with production-like schema) → Pre-deploy migrate (expand-phase additive changes) → Deploy application → Post-deploy verify → Post-deploy contract (cleanup, manual trigger).

**Who runs the migration:** In Kubernetes, use a **Helm pre-hook Job** (runs once, separate lifecycle, can be monitored). When CI has DB access, run as a **CI/CD pipeline step**. Never run migrations at application startup in multi-replica deployments — race conditions cause corruption. Always implement advisory lock mechanisms to prevent concurrent migration execution, and set lock timeouts to avoid stuck locks.

**Separate schema migrations from data migrations.** Schema migrations should complete in seconds and run in the deploy pipeline. Data migrations can take hours and must run asynchronously with batching, checkpointing, replication lag monitoring, and idempotency. Never reference ORM models in migration files — models change over time and break old migrations.

**Forward-only (fix-forward) is preferred** for production. Rollback scripts are unreliable for destructive changes and cannot handle new data written since migration. Use expand-contract for inherent rollback safety. Always take a verified backup before any destructive migration.

---

## 4. Security scanning: shift-left defense across the supply chain

### 4.1 Dependency vulnerability scanning

**Run Trivy as the primary scanner on every PR and push:** `trivy fs --exit-code 1 --severity CRITICAL,HIGH --ignore-unfixed .` Trivy is the industry-standard open-source scanner with **32,000+ GitHub stars**, scanning OS packages, language dependencies, container images, IaC, and generating SBOMs. Add language-specific complementary scanners: **govulncheck** for Go (performs call-graph reachability analysis), **pip-audit** for Python, **npm audit** for Node.js. For enterprises with budget, add **Snyk** for reachability analysis and automated fix PRs — it detects issues an average of **47 days faster** than NVD alone.

**Scan frequencies:** Every PR/push (Critical/High, exit on failure). Daily scheduled scan at 06:00 UTC (catches newly disclosed CVEs). On merge to main (full scan, generate SBOM). Weekly (full dependency audit including transitives).

**Always scan lock files**, not just manifests. Lock files (`package-lock.json`, `poetry.lock`, `go.sum`, `Cargo.lock`) contain exact resolved versions including transitive dependencies. Scanning manifests alone misses transitive vulnerabilities. Lock files must be committed to version control.

**Automate dependency updates with Renovate** (preferred for complex setups — 90+ package managers, cross-platform, grouped PRs) or **Dependabot** (GitHub-only, zero-config). Auto-merge patch/minor updates for stable (≥1.0) dependencies if tests pass. Require manual review for major versions. Set PR rate limits (10 concurrent max).

### 4.2 Container image scanning and hardening

**Base image priority order:** (1) **Distroless** (Google's `gcr.io/distroless`) — no shell, no package manager, dramatically reduced attack surface. (2) **Chainguard Images** — zero-CVE-at-build-time. (3) **Alpine** — small but has a package manager. (4) **Slim variants** — only for build stages, never production.

**Multi-stage builds are mandatory.** Build dependencies (compilers, dev tools) must not appear in the final image. Run as non-root user (`USER nonroot` or numeric UID). For Go static binaries, use `FROM scratch`. Use Docker BuildKit's `--mount=type=secret` for build-time secrets — never `ARG` or `ENV` for secrets (they persist in image layers).

**Always pin base images by digest** in production: `FROM node:18-alpine@sha256:abc123...`. Tags are mutable — an attacker can push a compromised image to the same tag. Never use `:latest` in production Dockerfiles or Kubernetes manifests. Use Renovate to automatically update digest pins.

**Sign every production image** with Cosign keyless signing (OIDC-based, eliminates key management): `cosign sign --yes ghcr.io/myorg/myapp:v1.2.3`. Verify signatures before deployment. Use **Kyverno or OPA Gatekeeper** as Kubernetes admission controllers to reject unsigned images. Attach SBOM attestations: `cosign attest --predicate sbom.json --type cyclonedx`.

**Scan at three locations:** CI pipeline post-build (gate on Critical/High), container registry (continuous, alert on new CVEs), and runtime/in-cluster (Trivy Operator with CRDs).

### 4.3 SAST, DAST, and secret scanning

**Pipeline placement:** Code push → SAST + Secret scan → SCA/dependency scan → Build → Container scan → IaC scan → Deploy to staging → DAST → Quality gate → Production.

**SAST tool selection:** Security-first and fast → **Semgrep** (YAML rules, taint tracking, 10s median CI scan). Deep semantic analysis on GitHub → **CodeQL**. Code quality + security → **SonarQube**. SAST must complete in <2 minutes for PR checks. Block PRs on Critical/High findings. Medium/Low findings are reported as comments but do not block.

**Secret scanning uses three layers.** Layer 1: Pre-commit hooks with **Gitleaks** (fast, low false positives). Layer 2: CI pipeline scanning with **TruffleHog** (800+ detectors, validates if secrets are live). Layer 3: Full git history scanning weekly with `gitleaks detect --source=. --log-opts="--all"`.

**When a secret is found, the remediation workflow is:** (1) Rotate/revoke the secret immediately. (2) Remove from codebase. (3) Force-push to clean git history if possible. (4) Check audit logs for unauthorized usage. (5) Update all systems using the old secret. (6) Document the incident. Removing a secret from code without rotating it is **never sufficient** — the attacker already has it from git history. Use HashiCorp Vault, AWS Secrets Manager, or equivalent for runtime secret injection.

### 4.4 Supply chain security and SBOM management

**Target SLSA Level 2 minimum for all production artifacts.** SLSA v1.1 (current as of April 2025) defines four levels: L0 (no provenance), L1 (auto-generated provenance), L2 (hosted build service generates signed provenance — prevents developer workstation attacks), L3 (hardened, isolated, ephemeral build environments). Use `slsa-github-generator` or `actions/attest-build-provenance` for SLSA provenance generation. Keyless signing via Sigstore (Fulcio + Rekor transparency log) eliminates private key management.

**Pin GitHub Actions by commit SHA**, not tag: `uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683`. Per Datadog's 2026 report, **71% of organizations** never pin by hash. The GhostAction attack of early 2025 compromised a GitHub Action affecting millions of weekly uses because repos pinned by tag.

**Generate SBOMs during build** for every release artifact using **Syft** (CycloneDX/SPDX) or Trivy. Choose **CycloneDX** for security/vulnerability management (OWASP-backed, native VEX support), **SPDX** for license compliance (Linux Foundation-backed, ISO standard). Standardize on one format. Sign SBOMs with Cosign. Feed into **Dependency-Track** for continuous portfolio-wide monitoring. NTIA minimum elements (per EO 14028): supplier name, component name, version, unique identifiers (purl), dependency relationships, SBOM author, and timestamp.

### 4.5 CVE prioritization and remediation SLAs

**Do not rely on CVSS alone.** Only **2.3% of CVEs** with CVSS ≥7 are actually observed being exploited. Use a modern prioritization stack: (1) **CISA KEV** catalog — if listed, it IS being exploited, treat as emergency. (2) **EPSS v4** (March 2025) — ML model predicting 30-day exploitation probability, scores 0–1. (3) CVSS v4 for technical severity context. (4) Reachability analysis. (5) Exposure context (internet-facing? identity system?). (6) Asset criticality.

**Remediation SLA timelines:**

| Tier | Criteria | Timeline |
|------|----------|----------|
| Emergency | KEV-listed, EPSS ≥0.7, confirmed exploitation | **Same day to 72 hours** |
| Accelerated | EPSS ≥0.1, public exploit, exposed systems | **7 days** |
| Standard | High CVSS without exploitation evidence | **30 days** |
| Routine | Medium severity, low exploitation probability | **90 days** |
| Accept risk | Low severity, mitigated by defense in depth | **Document and review annually** |

When accepting risk, document: CVE ID, CVSS/EPSS/KEV status, affected assets, mitigating controls, business justification, accepting party, expiration date (max 12 months), and quarterly review cadence. Track MTTR by severity tier, SLA compliance rate, EPSS ≥0.7 coverage, backlog age distribution, and dependency freshness (target <180 days behind latest).

---

## Conclusion: principles that tie it all together

Five cross-cutting principles unify these instructions. First, **instrument with open standards** — OpenTelemetry for telemetry, W3C Trace Context for propagation, CycloneDX/SPDX for SBOMs, SLSA for supply chain provenance. Vendor-specific tooling creates lock-in; open standards preserve portability. Second, **shift everything left but verify continuously** — scan dependencies and secrets in pre-commit hooks and CI, but also scan registries at rest and running workloads. New CVEs emerge daily against already-deployed code. Third, **alert on symptoms, investigate causes** — SLO burn-rate alerts catch user impact; dashboards and traces reveal root causes. Fourth, **make breaking changes incrementally** — the expand-contract pattern applies to database schemas, API contracts, and infrastructure changes alike. Never make a change that requires simultaneous coordinated updates across services. Fifth, **automate the boring parts ruthlessly** — dependency updates, SBOM generation, image signing, migration linting, and alert routing should never depend on human memory. The agent's job is to enforce these rules consistently, because the most dangerous failures come from the one time someone forgets.