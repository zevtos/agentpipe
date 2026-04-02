# The definitive production readiness checklist for 2026

**Every service shipped to production should pass four gates: reliability, security, architecture, and API design.** This reference compiles concrete, actionable checklist items across SRE production readiness, OWASP security compliance, 12-Factor App adherence, and API design best practices — drawn from Google's SRE books, OWASP Top 10:2025, the OWASP API Security Top 10:2023, IETF RFCs, Zalando/Stripe/Microsoft API guidelines, and the evolving 15/16-factor methodologies. Each item is written so a backend or infrastructure engineer can verify it before shipping.

---

## Part 1 — SRE production readiness review

### Observability: instrument the four golden signals with OpenTelemetry

Every production service must emit **latency** (histogram), **traffic** (request rate), **errors** (5xx count/rate), and **saturation** (CPU/memory/queue depth) — Google's four golden signals. Supplement with the RED method (Request rate, Error rate, Duration P50/P95/P99) per endpoint and the USE method (Utilization, Saturation, Errors) for infrastructure resources.

- [ ] Instrument all services with OpenTelemetry SDKs exporting via OTLP; deploy the OTel Collector in an agent+gateway pattern (sidecar/daemonset per node, centralized gateway for aggregation)
- [ ] Emit structured JSON logs with `trace_id`, `span_id`, `service_name`, and `request_id` on every log line; centralize in ELK, Loki, or Splunk with **< 30 s ingestion latency**
- [ ] Propagate W3C Trace Context headers (`traceparent`/`tracestate`) across all service boundaries — even non-instrumented services must forward these headers
- [ ] Enforce span naming conventions (e.g., `service.operation.resource`) and validate via CI linting; set high-cardinality guardrails alerting when a metric exceeds **> 1,000 unique label combinations**
- [ ] Build per-service dashboards showing golden signals with deployment annotations; run synthetic probes on critical user journeys (login, checkout, search) every **60 seconds** from multiple regions
- [ ] Enable auto-instrumentation for HTTP clients/servers, database drivers, and message queues for zero-code baseline visibility
- [ ] Implement tiered telemetry retention: **7-day** hot tier (fast query) → **30-day** warm → **90+ day** cold archive; apply PII redaction at the collector layer

### Alerting: page on SLO burn rates, not raw resource metrics

Alert quality directly determines on-call quality. The single most impactful change teams can make is shifting from cause-based alerts ("CPU > 90%") to **symptom-based SLO burn-rate alerts**.

- [ ] Configure multi-window, multi-burn-rate alerts per Google's recommended approach: **page on 2% budget burn in 1 hour (14.4× burn rate) and 5% in 6 hours (6×)**; ticket on 10% in 3 days (1×)
- [ ] Require every alert to have a linked runbook URL, an explicit team owner, and a quarterly review date; delete alerts that require no human action
- [ ] Deduplicate and group related alerts by service and failure mode; suppress child alerts when a parent service is already alerting
- [ ] Track pages per shift, MTTA (target: **< 5 min**), and percentage of pages resulting in meaningful action (target: **> 80%**); alert when night-shift pages exceed threshold
- [ ] Enrich alert payloads with recent deploy history, config changes, and infrastructure events to accelerate root-cause identification

### SLOs, SLIs, and error budgets as a decision framework

SLOs are not monitoring — they are a **decision framework** that determines when to ship features versus invest in reliability.

- [ ] Define **2–3 user-journey SLIs per service** using the ratio of good events to total events (e.g., "login success rate," "checkout latency P99 < 200 ms")
- [ ] Set tiered SLO targets by business criticality: Tier 1 (payment, auth) at **99.9%**, Tier 2 (core features) at **99.5%**, Tier 3 (internal tools) at **99.0%**; set internal SLOs stricter than contractual SLAs
- [ ] Display real-time error budgets on team dashboards — for a 99.9% SLO over 30 days, the budget is **43.2 minutes** of downtime or 0.1% of requests
- [ ] Implement an error budget policy: when budget < 25% remaining, freeze non-critical releases; when exhausted, halt all feature deployments until recovery
- [ ] Forecast budget exhaustion date using linear regression; alert when projected exhaustion falls within **7 days**; conduct quarterly SLO reviews and version-control all target changes

### Incident response: structured roles, blameless postmortems, tracked action items

- [ ] Define a severity matrix: **SEV-1** (> 25% users impacted or revenue-affecting), **SEV-2** (> 5% users or degraded critical feature), **SEV-3** (minor degradation, no revenue impact)
- [ ] Assign ICS-model incident roles: Incident Commander, Communications Lead, Operations Lead, Scribe; auto-create a dedicated Slack/Teams channel on SEV-1/SEV-2 declaration
- [ ] Require postmortem drafts within **5 business days** using a standard template: timeline with decision points, Five Whys root cause analysis, impact quantification, SMART action items with owners and due dates
- [ ] Enforce blameless culture: ban "human error" as root cause; track action item completion (target: **> 90%** on-time closure); share anonymized learnings in monthly reliability newsletters
- [ ] Measure MTTD, MTTA, MTTM, and MTTR separately; analyze incident recurrence quarterly by clustering failure modes

### Capacity planning, deployment strategy, and rollback readiness

- [ ] Set CPU/memory resource requests at P95 utilization and limits at 2× request for all containers; validate auto-scaling under simulated realistic load patterns (spikes, bursts, gradual ramp)
- [ ] Implement load shedding — return HTTP **429** or degrade gracefully when queue depth exceeds safe thresholds rather than cascading failures
- [ ] Deploy using canary with staged traffic shifting: **1% → 5% → 25% → 50% → 100%** with automated abort if error rate exceeds 2× baseline or P99 latency exceeds 1.5× baseline at any stage
- [ ] Separate deployment from release using feature flags; run automated post-deploy smoke tests; auto-rollback if any smoke test fails within **5 minutes**
- [ ] Maintain single-command rollback capability tested monthly; keep **N-1 through N-3** deployment artifacts in the registry; use expand-contract pattern for all database migrations

### Runbooks, chaos engineering, and on-call readiness

- [ ] Structure every runbook as: Trigger → Severity → Diagnostic Steps → Mitigation → Verification → Escalation; include exact commands (`kubectl rollout undo deployment/checkout --to-revision=3`), not descriptions, and direct links to dashboards
- [ ] Schedule quarterly Game Days for Tier 1 services injecting **3–5 failure scenarios** (instance termination, network partition, dependency timeout, disk full, DNS failure); validate that alerts fire, runbooks work, and on-call processes function end-to-end
- [ ] Implement two-level on-call rotation (primary + secondary); cap on-call load at **max 2 pages per 12-hour shift**; use shadow → co-pilot → primary progression for new engineers
- [ ] Standardize shift handoffs documenting active incidents, recent deploys, known risks, and error budget status; measure and reduce toil quarterly (target: **< 50%** of on-call work is toil)

---

## Part 2 — OWASP security checklist

### OWASP Top 10:2025 — what changed from 2021

The OWASP Top 10 was updated in **November 2025** with two significant additions: **A03: Software Supply Chain Failures** (expanded from "Vulnerable Components") and **A10: Mishandling of Exceptional Conditions** (new). SSRF was consolidated into A01: Broken Access Control, and Security Misconfiguration moved up to A02. The OWASP ASVS 5.0.0 was released in May 2025.

- [ ] Enforce server-side access control checks on every endpoint; deny by default unless explicitly granted (**A01** — Broken Access Control remains the #1 risk)
- [ ] Implement automated, repeatable hardening for all environments with identical configurations but different credentials (**A02**)
- [ ] Verify package integrity using checksums/signatures, pin dependency versions, and generate SBOMs for all releases (**A03** — new supply chain factor)
- [ ] Encrypt sensitive data at rest with **AES-256** and in transit with **TLS 1.2+**; never use MD5/SHA-1 for cryptographic purposes (**A04**)
- [ ] Use parameterized queries for all database interactions; validate and sanitize all user inputs server-side (**A05**)
- [ ] Conduct threat modeling during design phase; establish secure design patterns and abuse-case testing (**A06**)
- [ ] Implement MFA for all accounts; hash passwords with **Argon2id** (preferred), bcrypt, or scrypt (**A07**)
- [ ] Sign all build artifacts and verify signatures before deployment; verify CI/CD pipeline integrity (**A08**)
- [ ] Centralize logging with tamper-evident storage; configure alerts for authentication failures and access control violations (**A09**)
- [ ] Fail closed on errors; return generic messages to users while logging details internally; never expose stack traces or internal paths (**A10** — new)

### Authentication and authorization hardened for 2026

**RFC 9700** (OAuth 2.0 Security Best Current Practice), published January 2025, now mandates PKCE for all OAuth clients and deprecates the implicit grant entirely.

- [ ] Use Authorization Code + PKCE for all OAuth 2.0 flows (browsers, SPAs, native apps); PKCE is mandatory per RFC 9700
- [ ] Issue access tokens with **5–15 minute** TTL; implement refresh token rotation invalidating old tokens immediately upon use
- [ ] Sign JWTs with asymmetric algorithms (**RS256 or ES256**); validate `iss`, `aud`, `exp`, `nbf` claims on every request
- [ ] Set session cookies with `Secure`, `HttpOnly`, `SameSite=Lax` (or `Strict`); enforce absolute session timeout (**8 hours**) and idle timeout (**30 minutes**)
- [ ] Support **WebAuthn/passkeys** as primary passwordless authentication; fall back to TOTP-based MFA for privileged accounts

### Security headers every response must include

- [ ] `Strict-Transport-Security: max-age=31536000; includeSubDomains; preload` — submit to the HSTS preload list after verifying all subdomains work over HTTPS
- [ ] `Content-Security-Policy` starting with `default-src 'none'`; use **nonces or hashes** for inline scripts (never `'unsafe-inline'`); deploy in report-only mode for 1–2 weeks before enforcing
- [ ] `X-Content-Type-Options: nosniff` and `X-Frame-Options: DENY` (plus `frame-ancestors 'none'` in CSP)
- [ ] `Referrer-Policy: strict-origin-when-cross-origin` and `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()`
- [ ] `Cross-Origin-Opener-Policy: same-origin` and `Cross-Origin-Embedder-Policy: require-corp` for cross-origin isolation; remove `Server` and `X-Powered-By` headers

### Secrets management, dependency scanning, and TLS configuration

- [ ] Store all secrets in a dedicated secrets manager (Vault, AWS Secrets Manager, GCP Secret Manager); inject at runtime via sidecar or CSI driver — never bake into images or commit to Git
- [ ] Automate secret rotation on ≤ **90-day** cycles (≤ 60 days for high-security); use dynamic/ephemeral secrets with **1–24 hour TTL** where possible
- [ ] Run pre-commit hooks (gitleaks, truffleHog) and CI scanning (GitGuardian) to block secrets before they reach repositories; auto-revoke exposed credentials
- [ ] Generate SBOMs in **CycloneDX or SPDX** format on every build; run SCA in CI (Dependabot, Snyk, Grype, Trivy); fail builds on critical/high CVEs with no waiver
- [ ] Enforce maximum vulnerability age: critical **48 hours**, high **7 days**, medium **30 days**, low **90 days**
- [ ] Set minimum TLS version to **1.2**; enable **TLS 1.3**; restrict TLS 1.2 to AEAD cipher suites with forward secrecy (ECDHE-*-GCM-*); automate certificate renewal via ACME with **90-day** maximum lifespan
- [ ] Enable OCSP stapling; redirect all HTTP to HTTPS with 301; test configuration targeting **SSL Labs A+** rating

### OWASP API Security Top 10:2023 — the most exploited API weaknesses

**Broken Object Level Authorization (BOLA)** accounts for roughly **40% of API attacks**, making it the single most critical API security control.

- [ ] Enforce object-level authorization on every endpoint accessing a resource by ID; verify the authenticated user has permission to access the specific object
- [ ] Implement property-level authorization: use response DTOs to expose only necessary fields — never return all database columns; prevent mass assignment by allowlisting writable properties
- [ ] Protect sensitive business flows (purchasing, booking) from automated abuse with anti-bot measures on critical endpoints
- [ ] Prevent SSRF by validating all user-supplied URLs against domain allowlists; disable `file://` and `gopher://` schemes; isolate URL-fetching services in separate network segments
- [ ] Maintain a complete inventory of all API endpoints including versions, internal/external status, and deprecation state; decommission old API versions proactively

---

## Part 3 — 12-Factor App compliance (with modern extensions)

### The original twelve factors, modernized for Kubernetes

The 12-Factor App methodology, originally published by Heroku in 2011, was **open-sourced in November 2024** for community evolution. The core principles remain sound, but modern interpretations are significantly shaped by containers, Kubernetes, and GitOps.

**Factor 1 — Codebase.** Each deployable unit maps to exactly one Git repository (or a clearly separated path in a monorepo). All deployments originate from this single codebase; differences between environments are managed exclusively via configuration. Tag every production release with semantic versioning; extract shared code into versioned libraries distributed via package managers.

**Factor 2 — Dependencies.** Pin all dependency versions in lockfiles (`package-lock.json`, `poetry.lock`, `go.sum`). Specify exact base image digests in Dockerfiles. Generate an SBOM on every build and scan for CVEs in CI. The application must never rely on system-wide packages — everything runs in isolated containers with all dependencies bundled.

**Factor 3 — Config.** All config values varying between deployments are injected via environment variables, ConfigMaps, or Secrets — with **zero hardcoded credentials** in source code. The litmus test: the codebase could be open-sourced at any moment without compromising any credential. Applications fail fast at startup if required configuration is missing. Secrets are managed via dedicated vault systems with automated rotation.

**Factor 4 — Backing services.** Databases, caches, queues, and third-party APIs are referenced exclusively via URLs/connection strings in configuration — swappable without code changes. Circuit breakers (Resilience4j, Istio) protect all external calls with configurable thresholds and fallback responses. Retry logic uses exponential backoff with jitter.

**Factor 5 — Build, release, run.** The CI/CD pipeline enforces three immutable stages: Build → Release → Run. The same container image promotes across environments (never rebuilt per environment). Every release has a unique identifier (semver + Git SHA). Rollback is a first-class operation achievable in **< 5 minutes** via GitOps tooling.

**Factor 6 — Processes.** Application processes are stateless and share-nothing. Session state is externalized to Redis or a database. Verify by killing a random pod mid-session — user experience should be unaffected. No local file writes for application data; container filesystems are ephemeral.

**Factor 7 — Port binding.** The application embeds its own HTTP server, binds to a configurable port via environment variable, and does not rely on an external application server. Kubernetes Services handle internal routing; Ingress Controllers or Gateway API handle external traffic and TLS termination.

**Factor 8 — Concurrency.** Scale horizontally by running multiple identical stateless instances via HPA. Separate workload types (web, worker, scheduler) into distinct process types, each independently scalable. Use KEDA for event-driven autoscaling (queue depth, message rate). Validate scaling behavior with load tests before production.

**Factor 9 — Disposability.** Startup time under **10 seconds**. Graceful shutdown on SIGTERM: stop accepting requests, drain in-flight work within `terminationGracePeriodSeconds`, close connections, flush buffers. Configure separate liveness probes (no dependency checks) and readiness probes (check critical dependencies). Set Pod Disruption Budgets for minimum availability.

**Factor 10 — Dev/prod parity.** All environments use the same backing service types and versions (no SQLite in dev if production runs PostgreSQL). Infrastructure is defined as code (Terraform, Pulumi) with the same modules across all environments. Create ephemeral preview environments per pull request to catch drift early.

**Factor 11 — Logs.** Write all output to stdout/stderr in structured JSON. Include correlation IDs / OpenTelemetry trace IDs in every entry. Never log passwords, tokens, or PII. The platform handles collection, aggregation, and retention — no log file management in application code.

**Factor 12 — Admin processes.** Database migrations, data seeding, and one-time scripts run as Kubernetes Jobs or init containers using the same codebase and container image as the running application. All admin tasks are idempotent. REPL/console access is RBAC-restricted and audited.

### Beyond twelve: factors 13–15 and Google's 16-factor model

Kevin Hoffman's "Beyond the Twelve-Factor App" (O'Reilly) introduced three additional factors now widely adopted. In October 2025, **Google Cloud proposed a 16-factor model** adding AI-specific considerations.

**Factor 13 — API-first design.** Define API contracts (OpenAPI, gRPC proto, AsyncAPI) before implementation and version-control them. Generate mock servers for parallel development and run consumer-driven contract tests (Pact, Schemathesis) in CI to catch breaking changes. Every public endpoint has documented schemas, error codes, and rate limits.

**Factor 14 — Telemetry.** The three pillars of observability — structured logs, application metrics (RED/USE), and distributed traces — are first-class concerns, correlated via shared trace/span IDs using OpenTelemetry. Application-level metrics are exported via Prometheus `/metrics` or OTel Collector. Alerting is SLO-based, not raw-resource-based.

**Factor 15 — Authentication and authorization.** All endpoints require authentication (OAuth2, OIDC, mTLS) and authorization (RBAC/ABAC). Zero-trust networking uses mTLS via service mesh for service-to-service communication. Kubernetes ServiceAccounts have minimal RBAC roles. Container images run as non-root with read-only root filesystems. SAST, DAST, and container image scanning run on every pull request.

**Google's 16-factor additions (2025)** extend the model for AI/ML services: **Prompts as Code** (version-control prompt templates), **State as a Service** (externalize agent memory and context), **Observability for Non-Determinism** (trace LLM outputs, log confidence scores), and **Trust & Safety by Design** (content filtering, bias detection, guardrails).

---

## Part 4 — API design production readiness

### Versioning: choose one strategy and enforce it consistently

URI path versioning (`/v1/users`) remains the most widely adopted strategy for its visibility, cacheability, and ease of testing. Stripe's alternative — **date-based versioning** pinned per API key — works well for APIs with frequent, fine-grained changes.

- [ ] Apply semantic versioning to API releases: MAJOR for breaking changes, MINOR for additions, PATCH for fixes
- [ ] Maintain the previous major version for at least **12 months** after launching a new one
- [ ] Run automated backward compatibility checks in CI on every PR using OpenAPI diff tools (Specmatic, Optic); fail builds on detected breaking changes
- [ ] Only make additive changes within a major version: new optional fields, new endpoints, new optional parameters — never remove, rename, or change field types
- [ ] Implement consumer-driven contract testing (Pact, Spring Cloud Contract) so providers verify against consumer expectations before deploying

### Pagination, errors, and rate limiting done right

**Cursor-based pagination** avoids the O(n) scan problem and prevents skipped/duplicate records that plague offset pagination on changing datasets. **RFC 9457** (formerly RFC 7807) defines the standard error format. The **IETF RateLimit header draft** (draft-ietf-httpapi-ratelimit-headers-10) is converging toward standardization with Cloudflare already implementing it.

- [ ] Use cursor-based (keyset) pagination for large, dynamic datasets; return opaque cursor tokens (base64-encoded), not raw database IDs; enforce a server-side maximum page size (e.g., **limit=100**)
- [ ] Return RFC 9457-compliant errors (`application/problem+json`) with `type` (documentation URI), `title`, `status`, `detail`, and `instance` fields on every 4xx/5xx response; include a `traceId` extension for debugging
- [ ] Never expose stack traces, database errors, or internal paths in production error responses; maintain a machine-readable error codes catalog
- [ ] Adopt IETF `RateLimit-Policy` and `RateLimit` headers alongside legacy `X-RateLimit-*` headers for backward compatibility; return **429** with `Retry-After` in delta-seconds (never Unix timestamps)
- [ ] Use sliding window algorithms over fixed windows; implement tiered limits (per-user, per-IP, global); return rate limit headers on **all** responses, not just 429s

### Idempotency and the Idempotency-Key header

The IETF draft (draft-ietf-httpapi-idempotency-key-header-07) formalizes what Stripe has practiced for years: clients include an **`Idempotency-Key`** header on mutating requests, and the server stores and replays the original response on duplicate submissions.

- [ ] Accept `Idempotency-Key` on all POST/PATCH endpoints; store the key and original response server-side; return stored response on replays without re-execution
- [ ] Set key expiration at **24–48 hours** (Stripe uses 24h, Adyen uses 48h); return **422** if a key is reused with different parameters; return **409** if a concurrent request with the same key is still processing
- [ ] Include `X-Idempotent-Replay: true` on replayed responses so clients can distinguish cached from fresh responses
- [ ] Validate request fingerprints: compare incoming parameters against the original request for a given key and reject mismatches

### OpenAPI documentation, deprecation, and health checks

- [ ] Use **OpenAPI 3.1+** (JSON Schema draft 2020-12 compatible); adopt design-first workflow where the spec is written before implementation, committed to source control, and used to drive code generation, testing, and docs
- [ ] Provide `summary`, `description`, and realistic `example` values for every operation; lint specs in CI with Spectral or Redocly CLI; generate client SDKs from the spec using OpenAPI Generator or Stainless
- [ ] Send the `Deprecation` header (**RFC 9745**) with timestamp and the `Sunset` header (**RFC 8594**) with the shutdown date on deprecated endpoints; include `Link` header with `rel="successor-version"` pointing to migration docs
- [ ] Provide a minimum **6–12 month** deprecation window; monitor usage of deprecated endpoints and contact active consumers before sunset
- [ ] Implement three health endpoints: `/health/live` (lightweight, no dependency checks — for liveness probes), `/health/ready` (checks critical dependencies — for readiness probes), `/health/startup` (initialization complete — for startup probes)
- [ ] Return health responses in `application/health+json` format (IETF draft) with `status` (`pass`/`fail`/`warn`) and component-level checks including `observedValue` and `observedUnit`
- [ ] Secure detailed health endpoints with authentication; expose only basic status (200/503) on unauthenticated paths; set `Cache-Control: max-age=5` to prevent overwhelming health endpoints

### Cross-cutting API concerns

Every API response should include a **correlation ID** (`X-Request-Id`) propagated through all downstream services and included in error responses and log entries. Set `ETag` and `Cache-Control` headers on cacheable GET responses. Return proper HTTP status codes consistently: **201** with `Location` header for resource creation, **204** for successful deletions, **409** for conflicts, **422** for validation failures.

---

## Conclusion

These four checklists intersect more than they diverge. SLO burn-rate alerting (SRE) depends on the same OpenTelemetry instrumentation mandated by Factor 14 (telemetry). The OWASP supply chain factor (A03:2025) aligns with Factor 2's dependency management. RFC 9457 error responses serve both API design quality and security logging (OWASP A09). The most effective production readiness process treats these not as four separate reviews but as **one integrated gate** — a service that passes all four domains ships with confidence.

Three patterns stand out across domains. First, **automation beats documentation**: automated canary abort criteria, automated secret rotation, automated backward-compatibility checks, and automated SBOM generation all outperform manual processes. Second, **fail closed by default** — deny access unless explicitly granted (OWASP A01), reject requests missing required headers (idempotency keys), and halt deployments when error budgets are exhausted. Third, **measure what matters**: SLO error budgets, DORA metrics, vulnerability age policies, and contract test coverage provide objective signals that replace subjective "readiness" assessments with quantifiable gates.

The freshest developments to watch: the OWASP Top 10:2025 adding supply chain and exception handling as formal categories, RFC 9700 making PKCE mandatory for all OAuth flows, the IETF RateLimit header draft approaching standardization, Google's 16-factor model adding AI-specific factors, and the 12factor.net open-sourcing enabling community-driven evolution of the methodology.