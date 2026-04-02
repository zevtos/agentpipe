---
description: "Architecture planning for a feature or system change. Use /kickoff for new projects, /feature for full end-to-end development."
argument-hint: <what to plan>
---

You are creating an architecture plan. This is the PLANNING ONLY command — it does NOT implement anything. Use /feature if you want end-to-end development including implementation.

## Context
@CLAUDE.md

## What to Plan
$ARGUMENTS

## Pipeline

### Step 1: Understand
Read the codebase to understand:
1. Current architecture and patterns
2. How the requested change fits into existing design
3. What components are affected
4. What constraints exist (tech stack, conventions, existing APIs)

### Step 2: Design (Architect Agent)
Run the `architect` agent:
"Plan the implementation of: $ARGUMENTS

Analyze the current codebase, then produce:
1. Component diagram showing affected modules
2. API contract changes (if any endpoints change)
3. Data model changes (if schema changes needed)
4. Implementation steps in dependency order
5. ADR for any significant technical decisions
6. Risk assessment
7. Estimated complexity: SMALL (1-2 files) / MEDIUM (3-5 files) / LARGE (5+ files)"

### Step 3: Breakout
Break the plan into atomic, committable units:
- Each unit should be independently deployable if possible
- Each unit maps to one conventional commit
- Dependencies between units are explicit

### Step 4: Present

```
## Implementation Plan: $ARGUMENTS

### Architecture Changes
[Diagram and description]

### Implementation Steps
1. [step] — `commit type: description`
2. [step] — `commit type: description`
...

### ADRs
[Any architecture decisions]

### Risks
[What could go wrong]

### Recommendation
- Complexity: [SMALL | MEDIUM | LARGE]
- Suggested approach: [implement directly | use /feature for full pipeline | break into sub-tasks]
```

Ask: "Plan ready. Want me to implement it? I can use /feature for the full pipeline or start implementing directly."
