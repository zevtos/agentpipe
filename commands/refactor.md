---
description: "Safe refactoring workflow with architecture review, implementation, and verification gates. Ensures refactors don't break existing behavior."
argument-hint: <what to refactor and why, or area of code>
---

You are orchestrating a safe refactoring. The #1 rule: refactoring must not change external behavior. Every step must preserve the existing test suite passing.

## Context
@CLAUDE.md

## Refactoring Goal
$ARGUMENTS

## Pipeline

### Step 1: Assess Scope
Before any changes:
1. Read the code to be refactored — understand ALL its responsibilities
2. Map all callers/consumers (use Grep to find all references)
3. Check test coverage — run existing tests, note what's covered and what's not
4. Identify the risk: is this a leaf function or a core abstraction used everywhere?

Present:
- **Current state**: what the code does today and why it needs refactoring
- **Scope**: exact files and functions affected
- **Risk level**: LOW (leaf code, good tests) / MEDIUM (shared code, some tests) / HIGH (core abstraction, few tests)
- **Proposed approach**: how to refactor safely

Ask: "This is a [RISK] refactor. Proceed?"

### Step 2: Safety Net (Tester Agent — if needed)
If test coverage is insufficient for the refactored code, run the `tester` agent FIRST:
"Write characterization tests for the following code BEFORE refactoring:
[list files/functions being refactored]
These tests must capture the CURRENT behavior exactly — they serve as a safety net.
Focus on: all public API surfaces, edge cases, error paths."

Run the characterization tests to verify they pass.

### Step 3: Architecture Review (Architect Agent — if structural)
If the refactoring is structural (changing module boundaries, API contracts, data flow), run the `architect` agent:
"Review this refactoring plan:
Goal: $ARGUMENTS
Current structure: [describe]
Proposed structure: [describe]

Evaluate:
- Is this the right decomposition?
- Are the new boundaries clean?
- Does this create better or worse coupling?
- Any migration concerns?"

### Step 4: Implement
Refactor in small, tested steps:
1. Make one change at a time
2. Run tests after EACH change
3. If tests fail, revert the last change and investigate
4. Keep commits atomic — each commit should be a standalone refactoring step that passes all tests

### Step 5: Review (Reviewer Agent)
Run the `reviewer` agent:
"Review this refactoring of: $ARGUMENTS
Key verification:
- External behavior is UNCHANGED (no new features, no bug fixes mixed in)
- All existing tests still pass
- Code quality improved (the refactoring actually helps)
- No accidental API changes
- No accidental behavior changes hiding in the diff"

Fix any findings.

### Step 6: Verify
1. Run the FULL test suite (not just tests for changed files)
2. If the project has integration/e2e tests, run those too
3. Verify no test was modified to "make it pass" with new behavior

Present:
- What changed (structural overview)
- What did NOT change (externally visible behavior)
- Test results (all passing)
- Commits ready (one per refactoring step, conventional format: `refactor: ...`)
