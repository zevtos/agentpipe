---
description: "Generate or update project documentation: API docs, README, ADRs, runbooks, and architecture docs. Verifies everything against actual code."
argument-hint: [optional: 'api'|'readme'|'adr'|'runbook'|'all']
---

You are orchestrating documentation generation. Documentation must be accurate (verified against code), actionable (copy-pasteable commands), and maintainable.

## Context
@CLAUDE.md

## Doc Type
$ARGUMENTS

## Pipeline

### Step 1: Assess Current State
1. Check what documentation already exists (README, docs/, doc/adr/, CHANGELOG, API docs)
2. Check when docs were last updated vs when code was last changed
3. Identify stale documentation (docs that reference code that has changed)
4. Identify missing documentation (undocumented endpoints, modules without docs, missing runbooks)

Present: "Current documentation state: [summary]. Recommend updating: [list]."

### Step 2: Generate/Update (Docs Agent)
Based on the doc type requested (or 'all' if not specified):

**If 'api' or 'all':**
Run the `docs` agent:
"Generate comprehensive API documentation.
Read all API endpoints in the codebase. For each endpoint document:
- HTTP method and path
- Description (what it does and when to use it)
- Request parameters, body schema with types and constraints
- Response schema with example response
- All error responses with codes and trigger conditions
- Authentication requirements
- Rate limiting behavior
Verify every documented endpoint actually exists in code."

**If 'readme' or 'all':**
Run the `docs` agent:
"Update the project README.md.
Read the codebase to understand current state, then generate/update:
- Project description (one sentence)
- Quick start (3-5 steps that actually work — test them)
- Architecture overview
- Development setup (prerequisites, install, run, test)
- Deployment instructions
- Environment variables documentation
- Contributing guidelines"

**If 'adr' or 'all':**
Run the `docs` agent:
"Review the codebase for undocumented architectural decisions.
Look for: technology choices, patterns used, unusual configurations, commented explanations.
For each undocumented decision, generate an ADR in Nygard format.
Check existing ADRs in doc/adr/ or docs/adr/ — don't duplicate."

**If 'runbook' or 'all':**
Run the `docs` agent:
"Generate operational runbooks for this project.
Read the infrastructure code (Dockerfile, CI/CD, monitoring config), then create runbooks for:
- Service won't start (startup failures)
- High error rate (debugging production errors)
- High latency (performance degradation)
- Database issues (connection failures, slow queries)
- Deployment rollback procedure
Each runbook must have: trigger, severity, diagnostic commands, step-by-step remediation, escalation path."

### Step 3: Verify Accuracy
After generation, verify:
1. All code examples compile/run
2. All referenced file paths exist
3. All documented endpoints exist in code
4. All environment variables mentioned are actually used
5. Setup instructions produce a working development environment

Flag any unverifiable claims with `[NEEDS REVIEW]`.

### Step 4: Summary
```
## Documentation Report

### Generated/Updated
[List of documentation files created or modified]

### Verification Status
- [VERIFIED] items checked against code
- [NEEDS REVIEW] items requiring human verification

### Maintenance Notes
[When these docs will need updating — what code changes trigger updates]
```

Ask: "Documentation ready. Want me to commit? `docs: update [type] documentation`"
