---
description: "Comprehensive test strategy and implementation: analyzes codebase, identifies coverage gaps, writes tests, and validates quality with mutation testing."
argument-hint: [optional: specific module, file, or feature to test]
---

You are orchestrating comprehensive test coverage for the project. This goes beyond "write some unit tests" — it's a full test strategy with risk-based prioritization.

## Context
@CLAUDE.md

## Test Target
$ARGUMENTS

## Pipeline

### Step 1: Coverage Analysis
Before writing any tests:
1. Identify the testing framework in use (check package.json, pyproject.toml, Cargo.toml, etc.)
2. Run existing tests to establish baseline: what passes, what fails, what's slow
3. If coverage tooling exists, run it to identify untested code paths
4. Map critical code paths that MUST be tested (auth, payments, data mutations, business rules)
5. Identify which test types already exist (unit, integration, e2e)

Present:
- **Current coverage**: what's tested, what's not
- **Risk map**: critical untested code paths ranked by risk
- **Test strategy**: which types of tests to add and why

### Step 2: Test Implementation (Tester Agent)
Run the `tester` agent:
"Analyze the codebase and implement comprehensive tests.
Target: $ARGUMENTS (if empty, focus on highest-risk uncovered code)

Current test coverage: [paste from Step 1]

Write tests in this priority order:
1. **Critical business logic** — domain rules, calculations, state transitions
2. **API endpoints** — request validation, response format, error handling, auth
3. **Data layer** — queries return correct results, constraints enforced, migrations work
4. **Error paths** — what happens when things fail (network, DB, invalid input)
5. **Edge cases** — boundary values, empty inputs, concurrent access

For each test file:
- Follow existing test patterns and conventions
- Use real dependencies where possible (mock only external HTTP services)
- Name tests descriptively: test_[scenario]_[expected_result]
- Include setup/teardown for clean test isolation

Consider advanced strategies where they add value:
- Property-based testing for serialization/deserialization round-trips
- Property-based testing for algorithmic invariants
- Contract tests if there are service-to-service APIs
- Fuzzing for parsers or security-critical input processing"

### Step 3: Run and Verify
1. Run ALL tests (new and existing) — everything must pass
2. Fix any test failures
3. Check that new tests actually test something meaningful:
   - Temporarily break the code the test covers
   - Verify the test fails
   - Restore the code
4. Run coverage to show improvement

### Step 4: Quality Assessment
Present:
```
## Test Report

### Coverage Summary
- Before: [X% line coverage, Y critical paths untested]
- After: [X% line coverage, Y critical paths tested]

### Tests Added
| File | Tests | Type | What's Covered |
|------|-------|------|----------------|
| test_auth.py | 12 | unit+integration | Login, token refresh, expiry, MFA |
| ... | ... | ... | ... |

### Remaining Gaps
[Code paths still untested, with risk assessment and reason for deferral]

### Test Infrastructure Notes
[Any setup needed: test DB, fixtures, CI config changes]
```

Ask: "Tests pass and coverage improved. Want me to commit these? `test: add [description] tests`"
