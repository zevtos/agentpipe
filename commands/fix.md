---
description: "Bug investigation and fix workflow: investigate → diagnose → fix → review → regression test. Handles the full lifecycle from symptom to verified fix."
argument-hint: <bug description, error message, or symptom>
---

You are orchestrating a bug fix. Be systematic — don't jump to solutions before understanding the root cause.

## Context
@CLAUDE.md

## Bug Report
$ARGUMENTS

## Pipeline

### Step 1: Investigate
Before touching any code:
1. Reproduce the bug — understand exactly what triggers it
2. Read the relevant code paths (use Grep/Glob to find related files)
3. Check git blame/log for recent changes in the affected area
4. Read error logs if available
5. Identify the root cause — not just the symptom
6. Check if this is a known pattern (race condition? null reference? off-by-one? missing validation?)

Present your findings:
- **Symptom**: What the user sees
- **Root Cause**: Why it happens (be specific — point to exact code)
- **Impact**: How many users/flows are affected
- **Fix Strategy**: What needs to change and why

Ask: "Is this the right diagnosis? Should I proceed with this fix approach?"
Wait for confirmation.

### Step 2: Fix
Implement the fix:
- Make the minimal change that addresses the root cause
- Don't refactor surrounding code — fix the bug only
- Add input validation or guard clauses if the bug was caused by unexpected input
- Add logging if the bug was hard to diagnose (help future debugging)
- Handle the error gracefully if it's an edge case that can't be prevented

### Step 3: Review (Reviewer Agent)
Run the `reviewer` agent:
"Review this bug fix for: $ARGUMENTS
Root cause: [paste root cause from Step 1]
Changes made: [describe the fix]
Verify:
- Does the fix actually address the root cause?
- Does it introduce any regressions?
- Are edge cases handled?
- Is error handling appropriate?
- Could this bug manifest elsewhere? (similar patterns in the codebase)"

If the reviewer finds issues, fix them and re-verify.

### Step 4: Regression Test (Tester Agent)
Run the `tester` agent:
"Write a regression test for this bug: $ARGUMENTS
Root cause: [paste root cause]
The fix: [describe what changed]

Write tests that:
1. Reproduce the original bug (this test would have FAILED before the fix)
2. Verify the fix works correctly
3. Cover related edge cases that could cause similar issues
4. Follow existing test patterns in the project"

Run the tests to verify they pass. Also run the existing test suite to ensure no regressions.

### Step 5: Summary
Present:
- Root cause explanation (one sentence)
- What was changed (files, specific changes)
- Regression test added
- Confidence level: HIGH / MEDIUM / LOW with reasoning
- Related areas to watch: other code that might have the same pattern

Ask: "Ready to commit? Suggested commit message: `fix: [description based on root cause]`"
