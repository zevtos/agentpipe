# Agents Reference

Agents are specialist personas with bounded tool access. They are invoked by commands, not directly by users. Each agent has a specific domain of expertise and a strict set of tools.

---

## Agent Overview

| Agent | Domain | Model | Tools |
|-------|--------|-------|-------|
| **architect** | System design, API contracts, ADRs | opus | Read, Grep, Glob, WebFetch, WebSearch |
| **pm** | Requirements, specs, user stories | sonnet | Read, Grep, Glob, WebFetch, WebSearch |
| **dba** | Schema design, migrations, queries | sonnet | Read, Grep, Glob, Bash |
| **devops** | CI/CD, Docker, observability | sonnet | Read, Write, Edit, Bash, Glob, Grep |
| **reviewer** | Code review, quality gates | sonnet | Read, Grep, Glob, Bash |
| **security** | Threat modeling, vulnerability audit | opus | Read, Grep, Glob, Bash |
| **tester** | Test strategy, test implementation | sonnet | Read, Grep, Glob, Bash, Write, Edit |
| **docs** | API docs, ADRs, runbooks, changelogs | sonnet | Read, Write, Edit, Glob, Grep, Bash |

---

## Detailed Descriptions

### architect
System architect for designing production-grade software. Handles system decomposition, API contract design (OpenAPI 3.1+), data modeling, technology evaluation with trade-off matrices, and Architecture Decision Records (ADRs) in Nygard format.

**Used by**: `/kickoff`, `/plan`, `/feature`, `/next`, `/sprint`, `/refactor`, `/audit`

---

### pm
Product/project manager for requirements engineering. Turns vague ideas into precise specs with acceptance criteria (Given/When/Then), user stories, scope management, and risk identification.

**Used by**: `/kickoff`, `/feature`

---

### dba
Database architect and engineer. Designs normalized schemas, plans zero-downtime migrations (expand-contract pattern), optimizes queries via EXPLAIN analysis, and manages index strategy.

**Key standards**: BIGSERIAL/UUID for IDs, NUMERIC for money (never float), TIMESTAMPTZ (always UTC), JSONB for PostgreSQL.

**Used by**: `/kickoff`, `/db`, `/feature`*, `/sprint`* (*conditional — when schema changes detected)

---

### devops
DevOps engineer for infrastructure and deployment. Handles multi-stage Docker builds (distroless > alpine > slim), CI/CD with quality gates, OpenTelemetry observability, canary deployments, and security scanning (SAST, SCA, SBOM).

**Used by**: `/kickoff`, `/deploy`

---

### reviewer
Code reviewer focused on production-readiness. Checks correctness, error handling (specific exceptions, rollback, circuit breakers), performance (N+1 queries, unbounded ops, memory leaks), API contract compliance, and convention consistency.

**Used by**: `/feature`, `/fix`, `/sprint`, `/review`, `/db`, `/refactor`, `/deploy`

---

### security
Security engineer for threat modeling and vulnerability analysis. Performs STRIDE analysis, OWASP Top 10:2025 audit, dependency supply chain review, authentication/authorization review, and cryptographic implementation review.

**Used by**: `/audit`, `/review`, `/sprint`, `/feature`

---

### tester
Test engineer for comprehensive testing. Designs test strategy based on code type (unit for business logic, integration for APIs/DB, property-based for transformations, contract for integrations). Implements tests and validates with mutation testing.

**Used by**: `/feature`, `/fix`, `/sprint`, `/test`, `/db`, `/refactor`*

---

### docs
Documentation engineer for technical writing. Generates API docs (OpenAPI enrichment), ADRs, operational runbooks, changelogs from git history, and onboarding guides. Validates all documentation against actual code.

**Used by**: `/release`, `/docs`, `/feature`, `/deploy`
