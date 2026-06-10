---
description: "Turn specs and architecture into a sprint-ready GitHub backlog: epics, milestones, and atomic issues with acceptance criteria and dependency order. The bridge between /kickoff and /sprint."
argument-hint: "[optional: path to spec/architecture docs, or a scope like 'MVP']"
---

You are orchestrating backlog generation: converting planning artifacts (specs, architecture, ADRs — typically `/kickoff` output) into GitHub issues that `/sprint` can execute. Every issue must be implementable without re-reading the full spec: self-contained context, testable acceptance criteria, explicit dependencies.

## Context
@CLAUDE.md

## Source Material
$ARGUMENTS

## Auto-context
- Repo: !`gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo "NO GITHUB REMOTE"`
- Existing open issues: !`gh issue list --state open --limit 200 --json number,title --jq '.[] | "#\(.number) \(.title)"' 2>/dev/null`
- Existing milestones: !`gh api repos/{owner}/{repo}/milestones --jq '.[].title' 2>/dev/null`
- Existing labels: !`gh label list --json name --jq '.[].name' 2>/dev/null`

## Pipeline

### Step 1: Gather Source Material
Locate the planning artifacts:
1. If $ARGUMENTS names a path, read those documents.
2. Otherwise look for `/kickoff` outputs: spec documents, `docs/adr/` or `doc/adr/`, architecture docs, README architecture sections, project CLAUDE.md.
3. If no planning artifacts exist, stop: "No specs or architecture docs found. Run /kickoff first, or point me at the documents: /backlog <path>."

If there is no GitHub remote, stop: "Backlog needs a GitHub repo for issue creation. Create one (`gh repo create`) or add the remote first."

### Step 2: Decompose (PM Agent)
Run the `pm` agent:
"Decompose this planning material into a development backlog.
Source documents:
[paste spec + architecture content from Step 1]

Existing open issues (do NOT duplicate them):
[paste from auto-context]

Produce:
1. Epics — one per bounded context or major feature area, with a one-paragraph goal
2. Issues — atomic, independently deliverable units under each epic. Each issue must have:
   - Title: imperative, specific (`Add cursor pagination to GET /orders`, not `Pagination`)
   - Context: 2-3 sentences — enough to implement without reading the spec
   - Acceptance criteria: Given/When/Then, testable
   - Priority: MoSCoW (Must/Should/Could)
   - Size: S (hours) / M (a day) / L (multiple days — flag for splitting)
3. Split anything larger than L into multiple issues
4. Flag overlaps with existing open issues as SKIP with the existing issue number"

### Step 3: Order and Risk (Architect Agent)
Run the `architect` agent:
"Order this backlog for implementation.
Backlog: [paste PM output]
Architecture: [paste architecture summary from Step 1]

Produce:
1. Dependency graph: which issues block which (only real technical dependencies — schema before endpoints, auth before protected features)
2. Milestone grouping: Milestone 1 = walking skeleton (deployable end-to-end slice), then by MoSCoW priority and dependency order
3. Risk flags: issues needing a spike or ADR before implementation — mark them `needs-spike`
4. Sizing sanity check: flag PM estimates that look wrong given the architecture"

### Step 4: Present the Plan
Consolidate into a plan and present it:

```
## Backlog Plan

### Milestone 1: [name — the walking skeleton]
| # | Issue | Priority | Size | Blocked by | Labels |
|---|-------|----------|------|-----------|--------|

### Milestone 2: [name]
[same table]

### Skipped (already tracked)
[issue → existing #N]

### Needs decision
[ambiguous scope, conflicting requirements — anything the user must settle]
```

Ask: "Create [N] issues across [M] milestones on GitHub? Adjustments first?"
**Do NOT create anything before explicit confirmation** — issue creation is visible to the whole repo.

### Step 5: Create on GitHub
After confirmation:
1. Create missing labels (`gh label create`): `epic:<name>`, `priority:must|should|could`, `size:S|M|L`, `needs-spike`, plus `ready` for unblocked issues
2. Create milestones (`gh api repos/{owner}/{repo}/milestones -f title=...`)
3. Create issues in dependency order (blockers first, so their numbers exist):

```
gh issue create \
  --title "{TITLE}" \
  --milestone "{MILESTONE}" \
  --label "{labels}" \
  --body "$(cat <<'EOF'
## Context
{2-3 sentences from PM output}

## Acceptance Criteria
- [ ] Given {precondition}, when {action}, then {result}

## Dependencies
{Blocked by #N / None}

## Source
{spec/ADR reference}
EOF
)"
```

4. Issues whose blockers are all created get the `ready` label; blocked ones don't — `/sprint label:ready` then picks up only unblocked work.

### Step 6: Summary and Handoff
```
Backlog created.
  Milestone 1: [name] — [N] issues ([X] ready)
  Milestone 2: [name] — [N] issues
  Skipped duplicates: [count]
  Spikes needed: [list]

Next: /sprint label:ready
After each merged PR, re-label newly unblocked issues as ready (their blockers closed).
```
