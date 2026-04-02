---
description: "Iterative issue-by-issue development: plan → implement → self-review loop → fix → PR to develop. Works through all open issues one by one until each has a clean PR."
argument-hint: [issue numbers or labels, e.g. "12 15 20" or "label:ready" or empty for all open]
---

You are the **orchestrator**. Your job is coordination, not implementation. You read context, delegate to agents, collect their output, and act on it. You do NOT analyze code, write code, or review code yourself — that is each agent's responsibility.

The pipeline is strict. Every phase is mandatory. You may not skip or merge phases, even for trivial issues.

## Context
@CLAUDE.md

## Arguments
$ARGUMENTS

## Setup

Gather issues:

```
gh issue list --state open --assignee @me --json number,title,body,labels
```

If $ARGUMENTS specifies issue numbers (e.g. "12 15 20"), filter to those.
If $ARGUMENTS is a label (e.g. "label:bugs"), filter by label.
If $ARGUMENTS is empty, list all open and ask: "Which issues should I work through?"

Present list and wait for confirmation. Then process each issue with the loop below.

---

## Issue Loop (repeat for every issue)

### Phase 1: Read Issue

```
gh issue view {NUMBER} --json number,title,body,comments,labels
```

Collect: title, body, referenced files, labels. Do not interpret or plan — just gather facts.

If the issue is ambiguous or missing acceptance criteria, post a comment asking for clarification and pause until resolved.

---

### Phase 2: Architecture Plan — MANDATORY, NO EXCEPTIONS

**Always run the `architect` agent, even for one-line fixes. No skipping.**

Prompt to architect agent:
```
Issue #{NUMBER}: {TITLE}

Body:
{BODY}

Codebase context: [paste relevant file paths and signatures you found in Phase 1]

Your deliverables:
1. Root cause analysis (bug) OR feature gap analysis (feature)
2. Exact implementation plan — ordered list of file changes with specific line-level guidance
3. Data model changes (if any) — flag explicitly
4. API contract changes (if any) — flag explicitly
5. Risk assessment: what could break, what to test
6. Acceptance criteria: how to verify the fix is correct

Output format: numbered steps. Be specific — the implementor follows your steps literally.
```

Wait for architect output.

If architect returns GATE: FAIL or identifies unresolvable blockers:
- Post findings to the issue as a comment
- Skip this issue, move to next

If architect flagged data model changes → run `dba` agent before Phase 3:
```
Architect output for issue #{NUMBER} identified these data model changes:
[paste data model section from architect output]

Produce: expand-contract migration SQL, lock_timeout settings, rollback plan.
```

---

### Phase 3: Branch

```
git checkout develop && git pull origin develop
git checkout -b issue/{NUMBER}-{kebab-slug-max-5-words}
```

---

### Phase 4: Implement

You implement following the architect's ordered steps — **literally and completely**:
- Do exactly what the architect specified, no more, no less
- No scope creep, no adjacent fixes, no refactoring unrelated code
- If architect's plan is unclear, stop and re-run architect with a clarifying prompt
- Commit atomically per logical change:

```
git commit -m "fix(scope): description

Implements #{NUMBER}"
```

---

### Phase 5: Review Loop — BOTH AGENTS, EVERY ROUND

**Run `reviewer` and `security` agents in parallel. This is not optional.**

Prompt to `reviewer` agent:
```
Code review for issue #{NUMBER}: {TITLE}

Diff to review:
[run: git diff develop...HEAD and paste output]

Architect's implementation plan was:
[paste architect's ordered steps]

Review against the plan: did the implementation match the intent?
Also check: correctness, error handling, edge cases, performance, conventions.
Be strict — production code.
```

Prompt to `security` agent:
```
Security review for issue #{NUMBER}: {TITLE}

Diff to review:
[run: git diff develop...HEAD and paste output]

Check: injection risks, auth/authz, input validation, data exposure, error leakage, OWASP Top 10:2025, OWASP API Top 10:2023.
```

Collect all findings. Triage:
- CRITICAL / HIGH → fix before proceeding, no exceptions
- MEDIUM → fix if straightforward, else document in PR with follow-up issue
- LOW → document in PR

If any CRITICAL or HIGH findings:
1. Fix each finding
2. Commit: `fix: address {reviewer|security} findings for #{NUMBER}`
3. Re-run the agent that reported findings (on changed files only)
4. Repeat until both agents return GATE: PASS

Do NOT proceed to Phase 6 until both agents return GATE: PASS.

---

### Phase 6: Tests — TESTER AGENT

**Always run `tester` agent. You do not write tests yourself.**

Prompt to `tester` agent:
```
Issue #{NUMBER} is implemented and passed review.

Diff of changes:
[run: git diff develop...HEAD and paste output]

Architect's risk assessment was:
[paste risk section from architect output]

Write tests:
- Happy path
- Error cases from architect's risk assessment
- Edge cases and boundary values
- Regression: confirm the bug does not recur (for bug fixes)
Follow existing test patterns in this project.
```

After tester writes tests, run the full suite:
```
# discover test command from package.json / Makefile / pytest.ini / cargo.toml
```

If tests fail — fix the failure, do not proceed until green.

---

### Phase 7: PR to develop

```
gh pr create \
  --base develop \
  --title "fix(scope): {ISSUE_TITLE} (closes #{NUMBER})" \
  --body "$(cat <<'EOF'
## Summary
Closes #{NUMBER}

{2-4 bullets: what the problem was, what was changed, why}

## Changes
{file: one-line description}
{file: one-line description}

## Architect's implementation plan
{paste architect's ordered steps}

## Test plan
- [ ] Unit tests pass
- [ ] Integration tests pass
- [ ] {specific verification steps from architect's acceptance criteria}

## Review notes
{MEDIUM/LOW findings deferred: description + follow-up issue number}

🤖 Implemented via /sprint
EOF
)"
```

Comment on issue:
```
gh issue comment {NUMBER} --body "Implemented in PR #{PR_NUMBER}: {PR_URL}

Approach taken:
{2-3 sentences from architect's plan}

Key decisions:
{any non-obvious choices made}"
```

---

### Phase 8: Checkpoint

```
✓ Issue #{NUMBER} — {TITLE} → PR #{PR_NUMBER}
Agents used: architect → reviewer + security → tester
─────────────────────────────────────────────────
Next: Issue #{NEXT_NUMBER} — {NEXT_TITLE}
```

Proceed to next issue immediately unless user requested pause.

---

## Final Summary

```
Sprint complete.

Processed:
  ✓ #N  {title} → PR #X  [architect ✓ | reviewer ✓ | security ✓ | tester ✓]
  ✓ #M  {title} → PR #Y  [architect ✓ | reviewer ✓ | security ✓ | tester ✓]
  ✗ #K  {title} → BLOCKED: {reason} → commented on issue

PRs awaiting merge:
  {PR URLs}

Follow-up issues created:
  {list}
```
