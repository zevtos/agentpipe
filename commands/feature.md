---
description: "End-to-end feature development: spec → design → implement → review → test → docs. Pass a feature description and the orchestrator handles everything."
argument-hint: <feature description>
---

You are orchestrating end-to-end feature development. Follow this pipeline step by step. Each stage gates the next — do not proceed if a stage produces GATE: FAIL.

## Context
@CLAUDE.md

## Feature Request
$ARGUMENTS

## Pipeline

### Step 1: Specification (PM Agent)
Run the `pm` agent:
"Feature request: $ARGUMENTS

Read the existing codebase to understand context, then create a feature specification:
- User stories with acceptance criteria
- Edge cases and error scenarios
- Non-functional requirements with specific targets
- Impact on existing features
- Out of scope
- Open questions"

Present to user. Ask: "Does this capture the feature correctly? Any adjustments?"
Wait for confirmation before proceeding.

### Step 2: Design (Architect Agent)
Run the `architect` agent:
"Based on this feature specification:
[paste PM output]

Read the existing codebase architecture, then design this feature:
- Which existing components are affected?
- New components/modules needed
- API changes (new endpoints, modified contracts)
- Data model changes (new tables, altered columns, new indexes)
- Integration points
- ADR if a significant technical decision is required
- Implementation plan: ordered list of changes to make"

Present to user. Ask: "Good approach? Any concerns?"
Wait for confirmation.

### Step 3: Database Changes (DBA Agent — if needed)
ONLY if the architect identified data model changes, run the `dba` agent:
"Based on this design:
[paste relevant data model changes from architect output]

Read the existing schema, then:
- Design the migration with expand-contract pattern if breaking changes
- Provide exact SQL with safety measures (lock_timeout, CONCURRENTLY, NOT VALID)
- Specify rollback plan
- Identify index changes needed"

### Step 4: Implementation
Implement the feature following the architect's plan:
- Follow the ordered implementation steps from the design
- Apply the database migration from the DBA output if applicable
- Write the code following project conventions
- Add input validation and error handling
- Add structured logging for new operations

Do NOT write tests yet — that comes after review.

### Step 5: Review (Reviewer + Security Agents in Parallel)
Run BOTH agents in parallel:

**Reviewer agent:**
"Review the implementation of: $ARGUMENTS
Focus on: correctness, error handling, edge cases, performance, convention compliance.
Check the diff of all changes made."

**Security agent:**
"Review the security of the implementation for: $ARGUMENTS
Focus on: input validation, authentication/authorization, injection risks, data exposure, error information leakage."

Collect findings from both agents. If either returns GATE: FAIL:
- Fix ALL critical and high findings
- Re-run the failing agent on the specific fixes
- Repeat until GATE: PASS

### Step 6: Tests (Tester Agent)
Run the `tester` agent:
"Tests needed for: $ARGUMENTS
The implementation is complete and reviewed. Read the code, then:
- Design test strategy based on risk analysis
- Write tests: unit tests for business logic, integration tests for API endpoints
- Cover: happy path, error cases, edge cases, boundary values
- Consider property-based testing for data transformations
- Follow existing test patterns in the project"

Run the tests to verify they pass.

### Step 7: Documentation (Docs Agent)
Run the `docs` agent:
"Feature implemented: $ARGUMENTS
Update documentation:
- Update API documentation if new/changed endpoints
- Update README if new setup steps or features
- Add changelog entry
- Create ADR if architect recommended one
- Update runbooks if new operational procedures needed"

### Step 8: Summary
Present final summary:
- What was implemented (files created/modified)
- Test coverage summary
- Documentation updated
- Any remaining concerns or follow-up items
- Ready for commit? List the logical commits to make (conventional commit format)

Ask user: "Ready to commit? I'll create atomic commits following conventional commit format."
