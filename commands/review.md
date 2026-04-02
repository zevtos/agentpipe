---
description: "Comprehensive multi-agent code review: parallel reviewer + security audit on all changes, with actionable fix list and quality gate."
argument-hint: [optional: specific files or area to review]
---

You are orchestrating a comprehensive code review using multiple specialist agents in parallel. This is the quality gate before merging.

## Context
@CLAUDE.md

## What to Review
$ARGUMENTS

## Auto-context
- Status: !`git status --short`
- Changed files: !`git diff --name-only HEAD~1 2>/dev/null || git diff --name-only --staged 2>/dev/null || git diff --name-only`
- Diff stats: !`git diff --stat HEAD~1 2>/dev/null || git diff --stat --staged 2>/dev/null || git diff --stat`

## Pipeline

### Step 1: Determine Scope
If $ARGUMENTS specifies files or areas, review only those.
Otherwise, review ALL staged and unstaged changes (or the last commit if working tree is clean).

Read ALL changed files in full — not just the diff. Context matters.

### Step 2: Parallel Review (Reviewer + Security Agents)
Run BOTH agents simultaneously:

**Reviewer agent:**
"Review all code changes in this repository.
Changed files: [list from auto-context]

Perform a thorough review covering:
- Correctness: logic errors, edge cases, off-by-one, null handling
- Error handling: are all failure modes addressed? resources cleaned up?
- Performance: N+1 queries, unbounded operations, missing indexes, memory leaks
- API contracts: input validation, output format, status codes, idempotency
- Conventions: consistent with project patterns and CLAUDE.md rules
- Tests: are changes tested? are assertions meaningful?

For each finding, specify: file:line, severity, description, and specific fix."

**Security agent:**
"Security review of all code changes in this repository.
Changed files: [list from auto-context]

Check for:
- Injection vulnerabilities (SQL, command, XSS, template)
- Authentication/authorization gaps (missing checks, broken access control)
- Sensitive data exposure (logs, error messages, API responses)
- Cryptographic issues (weak algorithms, hardcoded keys, nonce reuse)
- Input validation gaps (missing validation, improper sanitization)
- Dependency risks (known CVEs, supply chain concerns)
- Security headers and CORS configuration
- Rate limiting on sensitive endpoints

For each finding, specify: severity, CWE reference, proof of concept, remediation."

### Step 3: Consolidate Findings
Merge findings from both agents into a single report, deduplicate, and sort by severity:

```
## Code Review Report

### Gate: [PASS | NEEDS_WORK | FAIL]

### Critical (must fix before merge)
[Merged findings from both agents]

### High (should fix before merge)
[Merged findings]

### Medium (fix soon)
[Merged findings]

### Low (nice to have)
[Merged findings]

### Security Summary
[Key security observations — both positive and negative]

### Positive Observations
[What's done well — reinforce good patterns]
```

### Step 4: Fix Guidance
If GATE is not PASS:
- List exact fixes needed, in priority order
- For each fix, explain WHY (not just what) so the developer understands the risk
- Offer to implement the fixes if the user approves

Ask: "Want me to fix the [N] critical/high findings? Or do you want to handle them?"
