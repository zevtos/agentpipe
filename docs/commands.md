# Commands Reference

Commands are orchestration pipelines that coordinate agents to complete complex workflows. Use them as `/command-name` in Claude Code.

---

## Lifecycle Commands

### `/kickoff` — Project Bootstrap
> Full project kickoff from an idea.

**Usage**: `/kickoff <project idea or description>`

Orchestrates 4 agents in sequence to produce a complete project foundation:
1. **PM agent** — requirements spec, user stories, acceptance criteria
2. **Architect agent** — system design, API contracts, ADRs
3. **DBA agent** — database schema, migrations
4. **DevOps agent** — CI/CD, Docker, infrastructure scaffold

Best for: starting a new project from scratch.

---

### `/next` — Strategic Advisor
> Analyzes full project state and recommends the most impactful next steps.

**Usage**: `/next`

No arguments needed. Runs two agents:
1. **Explore agent** (very thorough) — scans code, docs, git history, dependencies, CI
2. **Architect agent** — produces risk-ordered backlog with anti-recommendations

Output: "Project Pulse" report with prioritized actions and traps to avoid.

Best for: when you're unsure what to work on next.

---

### `/plan` — Architecture Planning
> Design-only planning for a feature or system change.

**Usage**: `/plan <what to plan>`

Runs the **architect agent** (mandatory) to produce:
- Component diagram of affected modules
- API and data model changes
- Implementation steps in dependency order
- ADRs and risk assessment

Does NOT implement anything. Use `/feature` for full development.

---

### `/onboard` — Project Orientation
> Understand a codebase's architecture, conventions, and current state.

**Usage**: `/onboard`

Explores the project systematically:
1. Tech stack, dependencies, CI/CD setup
2. Architecture map (entry points, modules, data model, APIs)
3. Development workflow (install, run, test, build, deploy)
4. Current state (TODOs, debt, recent activity)

Best for: entering a new project or returning after a break.

---

## Development Commands

### `/feature` — End-to-End Feature Development
> Complete feature lifecycle from spec to docs.

**Usage**: `/feature <feature description>`

8-step gated pipeline:
1. **PM agent** — spec with acceptance criteria
2. **Architect agent** — technical design
3. **DBA agent** — schema changes (only if architect flagged them)
4. **Implement** — code following architect's plan
5. **Reviewer + Security agents** — code review and security audit (parallel)
6. **Tester agent** — test implementation
7. **Docs agent** — documentation updates
8. Summary

Each gate must pass before proceeding.

---

### `/fix` — Bug Investigation and Fix
> Full lifecycle from symptom to verified fix.

**Usage**: `/fix <bug description, error message, or symptom>`

Pipeline:
1. Investigate — reproduce and diagnose root cause
2. Fix — implement minimal change
3. **Reviewer agent** — code review
4. **Tester agent** — regression tests
5. Summary with confidence level

---

### `/refactor` — Safe Refactoring
> Refactoring with architecture review and verification gates.

**Usage**: `/refactor <what to refactor and why>`

Pipeline:
1. Assess scope and test coverage
2. Write characterization tests if coverage is insufficient
3. **Architect agent** — review if structural change
4. Implement with verification

Ensures refactors don't break existing behavior.

---

### `/sprint` — Iterative Issue Development
> Works through GitHub issues one by one, creating PRs for each.

**Usage**: `/sprint [issue numbers or labels]`

Examples:
- `/sprint 12 15 20` — specific issues
- `/sprint label:ready` — by label
- `/sprint` — list all open, then choose

For each issue: read -> architect -> implement -> review + security -> test -> PR.

---

## Quality Commands

### `/review` — Multi-Agent Code Review
> Parallel reviewer + security audit on all changes.

**Usage**: `/review [specific files or area]`

Runs in parallel:
- **Reviewer agent** — correctness, error handling, performance, conventions
- **Security agent** — OWASP Top 10, injection, auth, crypto

Produces actionable fix list with quality gate (PASS/FAIL).

---

### `/test` — Comprehensive Testing
> Analyzes codebase, identifies coverage gaps, writes tests.

**Usage**: `/test [specific module or feature]`

Pipeline:
1. Coverage analysis — framework, baseline, critical paths
2. **Tester agent** — write tests (business logic, API, data, error paths)
3. Mutation testing validation

---

### `/audit` — Security Audit
> Full security assessment with threat modeling.

**Usage**: `/audit [specific area like 'auth', 'payments', 'api']`

Pipeline:
1. Reconnaissance — map attack surface
2. **Security agent** — STRIDE threat model, OWASP Top 10:2025, dependency audit
3. **Architect agent** — architecture review from security perspective

Produces prioritized findings report (CRITICAL > HIGH > MEDIUM > LOW).

---

## Operations Commands

### `/deploy` — Deployment Preparation
> Pre-flight checks, security scan, and deployment readiness gate.

**Usage**: `/deploy`

Pipeline:
1. Pre-flight checks (clean tree, tests pass, no debug code)
2. **DevOps agent** — security scan, dependency audit
3. **Reviewer agent** — final code review
4. **Docs agent** — changelog generation
5. Deployment readiness report (GO / NO-GO)

---

### `/release` — Release Management
> Version bump, changelog, tag creation.

**Usage**: `/release [version or 'patch'|'minor'|'major']`

Pipeline:
1. Determine version from commits or user input
2. **Docs agent** — changelog generation
3. Version bump and tag creation

---

### `/db` — Database Changes
> Safe schema migration workflow.

**Usage**: `/db <what database change is needed>`

Pipeline:
1. **DBA agent** — design with expand-contract analysis
2. Implement migrations
3. Code review
4. Integration test

Mandatory for any schema changes. Enforces zero-downtime safety.

---

### `/docs` — Documentation Generation
> Generate or update project documentation.

**Usage**: `/docs [type]`

Types: `api`, `readme`, `adr`, `runbook`, `all`

Runs the **docs agent** to generate/update documentation, then verifies everything against actual code. Flags unverifiable claims with `[NEEDS REVIEW]`.
