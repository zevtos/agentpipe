---
description: "Full security audit of the project or a specific area. Runs threat modeling, code audit, dependency scan, and produces a prioritized findings report."
argument-hint: [optional: specific area like 'auth', 'payments', 'api', or 'crypto']
---

You are orchestrating a comprehensive security audit. This goes deeper than a code review — it includes threat modeling, architecture-level analysis, and dependency auditing.

## Context
@CLAUDE.md

## Audit Scope
$ARGUMENTS

## Pipeline

### Step 1: Reconnaissance
Before invoking any agent, map the attack surface:
1. Identify all entry points (API endpoints, WebSocket handlers, webhooks, file uploads)
2. Identify sensitive data flows (auth tokens, PII, financial data, keys)
3. Identify third-party integrations and trust boundaries
4. Check for existing security controls (auth middleware, validation, rate limiting)
5. List all dependencies with versions

Present the attack surface map to the user.

### Step 2: Security Audit (Security Agent)
Run the `security` agent with the full scope:
"Perform a comprehensive security audit of this project.
Scope: $ARGUMENTS (if empty, audit the entire project)
Attack surface: [paste from Step 1]

Execute the full audit methodology:
1. STRIDE threat model on the architecture
2. OWASP Top 10:2025 checklist against the codebase
3. OWASP API Security Top 10:2023 if this is an API
4. Authentication and authorization flow review
5. Cryptographic implementation review (if applicable)
6. Input validation and output encoding review
7. Error handling and information leakage review
8. Session management review
9. Security header audit
10. Secret management audit (scan for hardcoded credentials)"

### Step 3: Dependency Audit
Run dependency scanning:
```
# Run available scanners
npm audit 2>/dev/null || pip-audit 2>/dev/null || cargo audit 2>/dev/null || true
```
If Trivy is available: `trivy fs --severity CRITICAL,HIGH .`

Cross-reference findings with:
- CISA KEV catalog (is any CVE actively exploited?)
- EPSS scores (what's the exploitation probability?)

### Step 4: Architecture Review (Architect Agent — security focus)
Run the `architect` agent:
"Review this system architecture from a SECURITY perspective only:
- Are trust boundaries correctly placed?
- Are service-to-service communications authenticated (mTLS, API keys)?
- Is the principle of least privilege followed for database access?
- Are there single points of failure in the auth chain?
- Is sensitive data encrypted at rest and in transit?
- Is there proper network segmentation?"

### Step 5: Consolidated Report
Produce a single security report:

```
## Security Audit Report
Date: [today]
Scope: $ARGUMENTS

### Executive Summary
[1-3 sentences: overall security posture and critical risk count]

### Risk Score: [CRITICAL | HIGH | MEDIUM | LOW]

### Findings by Severity
#### Critical ([count])
[Each with: location, description, impact, proof of concept, remediation, CWE/CVE]

#### High ([count])
[Same format]

#### Medium ([count])
[Same format]

#### Low ([count])
[Same format]

### Threat Model Summary
[STRIDE analysis results]

### Dependency Vulnerabilities
[CVE list with EPSS scores and remediation versions]

### Positive Security Controls
[What's implemented well]

### Recommendations
[Prioritized list of security improvements]

### Remediation Roadmap
1. Immediate (this sprint): [critical findings]
2. Short-term (next 2 weeks): [high findings]
3. Medium-term (next month): [medium findings + architectural improvements]
```

Ask: "Want me to fix the critical findings now? Or should I create issues for tracking?"
