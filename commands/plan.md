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

### Step 1: Design (Architect Agent) — MANDATORY
You MUST use the Agent tool with `subagent_type: "architect"` to run the architect agent. Do NOT skip this step. Do NOT substitute it with an Explore agent or your own analysis. The architect agent has specialized capabilities for system design.

Prompt for the architect agent:
"Plan the implementation of: $ARGUMENTS

Analyze the current codebase thoroughly, then produce:
1. Current architecture assessment — what exists, what patterns are used
2. Component diagram showing affected modules
3. API contract changes (if any endpoints change)
4. Data model changes (if schema changes needed)
5. Implementation steps in dependency order
6. ADR for any significant technical decisions
7. Risk assessment with mitigations
8. Estimated complexity: SMALL (1-2 files) / MEDIUM (3-5 files) / LARGE (5+ files)"

IMPORTANT: Wait for the architect agent to complete before proceeding. Use its output as the foundation for the plan.

### Step 2: Breakout
Break the plan into atomic, committable units:
- Each unit should be independently deployable if possible
- Each unit maps to one conventional commit
- Dependencies between units are explicit

### Step 3: Present

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
