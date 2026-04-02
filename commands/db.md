---
description: "Database change workflow: design → migration → review → test. Handles schema changes with zero-downtime safety."
argument-hint: <what database change is needed and why>
---

You are orchestrating a database schema change. Database changes are the most dangerous changes in a codebase — a bad migration can cause downtime or data loss. Follow this pipeline rigorously.

## Context
@CLAUDE.md

## Change Request
$ARGUMENTS

## Pipeline

### Step 1: Design (DBA Agent)
Run the `dba` agent:
"Database change needed: $ARGUMENTS

Read the existing schema and codebase, then:
1. Design the schema change with exact SQL
2. Classify: additive-only (safe) or breaking change (requires expand-contract)
3. For breaking changes: provide the full expand-contract plan (expand → migrate → contract)
4. Lock analysis: which locks are acquired, for how long, impact on concurrent queries
5. Index changes needed for new query patterns
6. Rollback plan: exact steps to undo if something goes wrong
7. Data migration plan if existing data needs transformation (batch size, checkpointing)
8. Impact on application code (which queries/models need updating)"

Present to user. Ask: "This migration plan look correct? Any concerns about the data transformation?"
Wait for confirmation.

### Step 2: Implementation
Based on the DBA output:
1. Create migration file(s) with exact SQL from the DBA plan
2. For expand-contract: create SEPARATE migration files for each phase
3. Update application code (models, queries, DTOs) to work with both old and new schema during transition
4. Add feature flag for read/write switching if applicable
5. Add data backfill script if needed (with batching, checkpointing, replication lag monitoring)

### Step 3: Review (Reviewer Agent)
Run the `reviewer` agent:
"Review this database migration for: $ARGUMENTS
Key checks:
- Migration SQL matches the DBA-approved plan
- lock_timeout is set before every DDL statement
- CONCURRENTLY used for index creation (PostgreSQL)
- NOT VALID + VALIDATE pattern for constraints
- Batch size appropriate for data migrations
- Rollback plan is executable
- Application code handles both old and new schema during transition
- No N+1 queries introduced by schema changes"

Fix any findings.

### Step 4: Test (Tester Agent)
Run the `tester` agent:
"Write tests for this database migration: $ARGUMENTS
Test:
1. Migration applies successfully on a clean database
2. Migration applies successfully on a database with existing data
3. Application works correctly with the new schema
4. Data migration transforms data correctly (if applicable)
5. Rollback works correctly
6. New constraints are enforced (try inserting invalid data)"

### Step 5: Execution Plan
Present the deployment plan:
- **Pre-migration**: checks to run (long-running queries, replication lag, backup status)
- **Phase 1 (Expand)**: additive changes, timing estimate
- **Phase 2 (Code deploy)**: application changes to dual-write
- **Phase 3 (Backfill)**: data migration timing and monitoring
- **Phase 4 (Contract)**: cleanup changes (schedule for 24-48h after Phase 2)
- **Monitoring**: what to watch during and after migration
- **Rollback trigger**: specific conditions that should trigger rollback

Ask: "Ready to commit the migration files? The execution plan above should be followed during deployment."
