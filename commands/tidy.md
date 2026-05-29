---
description: "Repository tidy-up: revalidate docs against the real repo and prune cruft, with git as the safety net — one reviewable commit, recoverable removals, never an irreversible delete. Hygiene only; code smells go to /refactor."
argument-hint: [optional: focus area — 'docs'|'artifacts'|'refs'|'all']
---

You are orchestrating a repository tidy-up: making the repo's own claims true again and removing cruft that should not be tracked. The whole point is that **git is the safety net** — every change lands in one reviewable commit, every removal is recoverable, and nothing irreversible happens without the user's word. This is hygiene, not refactoring: code smells and duplication go to `/refactor`, and history surgery (purging secrets or large blobs from past commits) is out of scope — only ever flagged, never performed.

## Context
@CLAUDE.md

## Focus Area
$ARGUMENTS

## Auto-context
- Tracked changes (must be empty to start): !`git status --porcelain --untracked-files=no`
- Everything present (untracked + ignored): !`git status --porcelain --untracked-files=all`
- Repo root: !`git rev-parse --show-toplevel 2>/dev/null || echo "NOT A GIT REPO"`

## Safety contract
Tidy classifies every change by reversibility and acts on its own only at the recoverable end:

| Change | Recoverable via | Tidy policy |
|---|---|---|
| Fix a false claim in a tracked file (count, dead internal link, renamed/removed reference) | the commit | apply |
| Untrack a generated file, keep it on disk (`git rm --cached`) + add the `.gitignore` rule | the commit | apply |
| Remove tracked junk from disk (`git rm`) | git history | apply |
| Remove **untracked** junk | a labelled `git stash` | stash, never `git clean` |
| Purge a large blob / secret from **history** (git-filter-repo, BFG) | — (rewrites history) | detect & hand off only |

Hard rules:
- **Clean tracked tree is a prerequisite** so the whole run is one revertible commit. If the tree is dirty, stop and ask the user to commit or stash first.
- **Never `git clean -f`.** Untracked files were never in history; deleting them is irreversible. Move them aside with a labelled `git stash` and report the name so the user can restore or drop it.
- **`.gitignore` only affects untracked files** — an already-committed artifact must be `git rm --cached`-ed in the same commit, or the rule is a no-op.
- **Secrets are not a cleanup task.** If a committed secret is found, STOP: tell the user to rotate/revoke it first (it survives in clones, forks, cached views, and PRs even after a rewrite), then hand off to git-filter-repo. Never rewrite history.
- **Fix only what is provably false.** Ambiguous cases (intentional external links, templated/example references, judgment calls) go to NEEDS REVIEW, not auto-fix.

## Pipeline

### Step 1: Prerequisite Gate
Confirm this is a git repo and the tracked tree is clean — no staged or unstaged changes to tracked files (untracked files are fine; they are tidy's targets). If the tree is dirty, stop: "Commit or stash your tracked changes first — tidy runs as one reviewable commit on a clean tree." Do not proceed.

### Step 2: Establish Ground Truth
Derive the repo's real state, because the docs can't be trusted for it: the actual inventory the docs enumerate (counts and lists of agents/commands/modules/etc.), what the build and tooling generate, and which paired files the project declares must stay in sync (per CLAUDE.md). This is the reality the docs get checked against. Scope to the Focus Area if one was given.

### Step 3: Detect & Correct (Docs Agent)
Run the `docs` agent:
"Audit this repository against its actual current state, using the ground truth from Step 1.

Apply directly (these are tracked-file edits that land in tidy's single commit): claims that are now FALSE — wrong counts or item lists, broken internal links and anchors, references to files/functions/flags/paths that were renamed or removed, setup/usage commands that no longer work, and sync-pair divergences. Correct each to match reality.

Propose only — never delete — everything else:
- **Cruft**: generated/build artifacts that are tracked or sitting in the tree, editor/OS files (`.DS_Store`, etc.), and `.gitignore` gaps where a generated pattern in the tree isn't ignored. Tag each as tracked (→ `git rm`/`git rm --cached`) or untracked (→ stash).
- **Hand-off**: large binaries or apparent secrets in history. Flag with the right tool; never act.

Report every applied fix as file:line (claimed → actual), every proposal, and anything judgment-dependent as NEEDS REVIEW."

### Step 4: Plan & Report
Consolidate into a plan, each item tagged with its safety tier:
```
## Tidy Plan

### Doc fixes (applied — truth corrections)
[file:line, claimed → actual]

### Cruft (proposed)
- untrack + ignore: [tracked generated files → git rm --cached + .gitignore rule]
- remove: [tracked junk → git rm]
- stash: [untracked junk → labelled git stash, recoverable]

### Hand-off (NOT done by tidy)
[history-level: large blobs / secrets — tool + the rotate-first warning]

### Needs review
[ambiguous — left for the human]
```

### Step 5: Apply Atomically (gated)
On approval of the plan:
1. `git rm --cached` the tracked generated files and add their `.gitignore` rules; `git rm` the tracked junk. (Doc fixes from Step 3 are already in the working tree.)
2. Untracked junk → `git stash push --include-untracked --message "tidy/<short-label>" -- <paths>`; record the stash name.
3. Stage everything and make ONE commit: `chore: tidy repo — revalidate docs, prune artifacts`.
4. Update `CHANGELOG.md` (`[Unreleased]`) if the project keeps one.

### Step 6: Final Confirmation
Show `git show --stat HEAD` plus the stash ref, if any. Ask:
"Tidy done in one commit. Untracked junk stashed as `tidy/<label>` (restore: `git stash apply`; drop: `git stash drop`). Keep it, `amend`, or `revert` (`git reset --hard HEAD~1`)?"
