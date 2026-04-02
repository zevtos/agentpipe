---
description: "Full deployment preparation: pre-flight checks, security scan, dependency audit, changelog, and deployment readiness gate."
---

You are orchestrating deployment preparation. This is the final gate before code reaches production. Be thorough — catching issues here is 100x cheaper than catching them in production.

## Context
@CLAUDE.md

## Auto-context
- Branch: !`git branch --show-current`
- Status: !`git status --short`
- Recent commits: !`git log --oneline -10`
- Uncommitted changes: !`git diff --stat`

## Pipeline

### Step 1: Pre-flight Checks
Run these checks sequentially, stopping on any failure:

1. **Clean working tree**: No uncommitted changes (warn if found)
2. **Tests pass**: Run the full test suite
3. **Build succeeds**: Run the project build command
4. **No debug artifacts**: Search for `console.log`, `debugger`, `binding.pry`, `import pdb`, `TODO`, `FIXME`, `HACK` in changed files since last release tag — warn if found
5. **Environment config**: Compare `.env.example` against actual env var usage in code — flag any undocumented variables

### Step 2: Security Scan (DevOps Agent)
Run the `devops` agent:
"Run a deployment security scan:
1. Dependency vulnerability scan (npm audit / pip-audit / cargo audit / trivy fs)
2. Check for hardcoded secrets (search for API keys, tokens, passwords in source)
3. Check Docker image if Dockerfile exists (base image pinned? multi-stage? non-root?)
4. Verify security headers are configured (CSP, HSTS, X-Frame-Options)
5. Check that production error handling doesn't leak stack traces
Report: [PASS / FAIL with findings]"

### Step 3: Reviewer Quick Check (Reviewer Agent)
Run the `reviewer` agent on changes since last release:
"Quick review of all changes since the last release tag.
Focus on: breaking API changes, data model changes that need migration, configuration changes that need deployment coordination.
This is NOT a full code review — just catch deployment-blocking issues."

### Step 4: Changelog (Docs Agent)
Run the `docs` agent:
"Generate a changelog for deployment.
Commits since last tag: !`git log $(git describe --tags --abbrev=0 2>/dev/null || echo HEAD~20)..HEAD --oneline`

Generate:
1. User-facing changelog (Added, Changed, Fixed, Breaking Changes)
2. Deployment notes (migrations to run, env vars to set, infrastructure changes)
3. Rollback plan (specific steps to undo this deployment)"

### Step 5: Deployment Readiness Report

```
## Deployment Readiness Report

### Gate: [READY | BLOCKED | WARNING]

### Pre-flight
- [ ] Clean working tree
- [ ] All tests pass ([X] passed, [Y] failed)
- [ ] Build succeeds
- [ ] No debug artifacts in production code

### Security
- [ ] No critical/high dependency vulnerabilities
- [ ] No hardcoded secrets
- [ ] Container hardened (if applicable)

### Changes Summary
- Commits: [count] since last release
- Files changed: [count]
- Breaking changes: [yes/no — list if yes]
- Database migrations: [yes/no — migration plan if yes]
- New environment variables: [list if any]
- Infrastructure changes: [list if any]

### Changelog
[Generated changelog]

### Deployment Steps
1. [Pre-deploy steps: migrations, config changes]
2. [Deploy command]
3. [Post-deploy verification: health checks, smoke tests]
4. [Monitoring: what to watch for 30 minutes after deploy]

### Rollback Plan
[Exact steps to rollback if issues found]
```

If GATE is BLOCKED, list the blocking issues and offer to fix them.
If GATE is WARNING, list concerns and ask: "Proceed with deployment despite warnings?"
If GATE is READY, ask: "Ready to deploy. Should I create a release tag?"
