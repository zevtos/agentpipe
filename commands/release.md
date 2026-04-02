---
description: "Cut a release: version bump, changelog generation, tag creation, and deployment preparation. Handles the full release lifecycle."
argument-hint: [optional: version like 'v1.2.0' or 'patch'|'minor'|'major']
---

You are orchestrating a release. This combines changelog generation, version management, and deployment readiness into one workflow.

## Context
@CLAUDE.md

## Release Target
$ARGUMENTS

## Auto-context
- Current version: !`git describe --tags --abbrev=0 2>/dev/null || echo "no tags yet"`
- Commits since last release: !`git log $(git describe --tags --abbrev=0 2>/dev/null || echo HEAD~50)..HEAD --oneline`
- Branch: !`git branch --show-current`

## Pipeline

### Step 1: Determine Version
If the user specified a version, use it.
If the user specified 'patch', 'minor', or 'major', calculate from current version.
If nothing specified, analyze commits to determine:
- Any `feat:` commits → minor bump
- Only `fix:` commits → patch bump
- Any `BREAKING CHANGE:` or `!:` → major bump

Present: "Proposing version [X.Y.Z]. Correct?"
Wait for confirmation.

### Step 2: Changelog (Docs Agent)
Run the `docs` agent:
"Generate a release changelog for version [version].
Commits: [paste commit list from auto-context]

Format:
## [version] - [today's date]

### Added
[New features from feat: commits]

### Changed
[Changes from refactor:/chore: commits that affect behavior]

### Fixed
[Bug fixes from fix: commits]

### Breaking Changes
[Breaking changes with migration instructions]

### Security
[Security fixes if any]

Write each entry as one user-facing sentence in past tense. Include PR/issue numbers.
Omit internal refactors that don't affect users."

Present the changelog. Ask: "Changelog look good? Any edits?"

### Step 3: Pre-release Checks
Run the same checks as /deploy:
1. All tests pass
2. Build succeeds
3. No critical dependency vulnerabilities
4. No uncommitted changes
5. On the correct branch (main/master)

### Step 4: Create Release
After all checks pass:
1. Update CHANGELOG.md (prepend new entry)
2. Update version in package.json / pyproject.toml / Cargo.toml / version file
3. Present the changes for review

Ask: "Ready to commit version bump and create tag [version]?"

On confirmation:
1. Commit: `chore: release [version]`
2. Create annotated tag: `git tag -a [version] -m "[version]: [one-line summary]"`
3. Present: "Release [version] tagged. Push with `git push origin main --tags` when ready."

Do NOT push automatically — let the user decide when.
