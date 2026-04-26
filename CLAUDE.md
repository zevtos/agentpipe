# agentpipe

Gated pipeline orchestration for Claude Code and Codex CLI: specialist agents, slash commands, and skills.
9 agents, 15 commands, 1 skill (`gost-report`), 14 research docs. Multi-target installer:
default puts everything in `~/.claude/`; `--target codex` puts skills in `~/.agents/skills/`
(open-agent-skills standard) and skips agents/commands (Codex format differences).
The only build step is packaging skills into release zips — no runtime, no tests, no dependencies.

## Commands

```
bash install.sh                       # install for Claude Code (default)
bash install.sh --target codex        # install for Codex CLI (skills only)
bash install.sh --dry                 # preview what would change (works with --target too)
bash install.sh --diff                # show repo vs installed differences
bash install.sh --pull                # copy installed back to repo
bash install.sh --uninstall           # remove installed files (target-scoped)
bash scripts/build-skills.sh          # package every skills/<name>/ into dist/<name>.zip
```

## Structure

```
agents/*.md             Agent definitions — YAML frontmatter + system prompt
commands/*.md           Slash commands — YAML frontmatter + orchestration pipeline
skills/<name>/SKILL.md  Skills — folder with SKILL.md plus optional scripts/, references/
research/*.md           Reference docs, numbered 01-14. Not auto-imported by agents.
docs/                   User-facing documentation (commands.md, agents.md, installation.md)
scripts/build-skills.*  Package skills/* into dist/*.zip for releases
.github/workflows/      release.yml: on tag push, builds skill zips and attaches to GH release
install.sh              Bash installer (macOS, Linux, WSL, Git Bash)
install.ps1             PowerShell installer (Windows)
VERSION                 Semver string, read by installers and release workflow
CHANGELOG.md            Keep a Changelog format
```

## Agent Format (agents/*.md)

YAML frontmatter then markdown body. Exact fields:

```
name:         lowercase identifier (architect, dba, devops, docs, pm, refactorer, reviewer, security, tester)
description:  trigger sentence with "MUST BE USED when/before..." conditions
tools:        comma-separated allowlist from: Read, Write, Edit, Bash, Glob, Grep, WebFetch, WebSearch
model:        opus (for architect, security) or sonnet (all others)
```

Tool scoping by role:
- Read-only + web research: `Read, Grep, Glob, WebFetch, WebSearch` (architect, pm)
- Read + diagnostic bash: `Read, Grep, Glob, Bash` (dba, reviewer, security)
- Full write access: `Read, Write, Edit, Bash, Glob, Grep` (devops, tester, docs)

Body structure:
- `# [Role] Agent` — H1 title
- `## Core Responsibilities` — numbered list
- Domain-specific sections — rules, checklists, decision tables
- `## Output Format` or equivalent (e.g. `## Specification Format`) — template the agent must follow
- `## Handoff Protocol` — `## Next Steps` with `RECOMMEND:` lines to other agents

## Command Format (commands/*.md)

YAML frontmatter then markdown body. Exact fields:

```
description:    quoted string, one sentence explaining the command
argument-hint:  (optional) placeholder shown to user, e.g. <feature description>
```

Body structure:
- `## Context` containing `@CLAUDE.md` — always present, loads project instructions
- `$ARGUMENTS` under a descriptive heading — present when argument-hint exists
- `## Pipeline` with `### Step N: Name` — sequential, gated steps (most commands)
- Loop-style commands (e.g. sprint) use `## Loop` with `### Phase N:` instead
- Agent invocation: `Run the [name] agent:` followed by prompt in quotes

Commands without argument-hint: next, onboard, deploy.
Commands that run agents in parallel: review (reviewer + security), feature step 5.

## Skill Format (skills/<name>/)

Each skill is its own folder under `skills/`. Required files at the folder root:
- `SKILL.md` — YAML frontmatter (`name` matching folder, `description` trigger sentence) + body
- `LICENSE` — every skill ships its own license so the standalone `.zip` is legally usable when uploaded to Claude Chat or extracted into `~/.claude/skills/`. Default is MIT (matches repo top-level)

Optional folders inside the skill:
- `scripts/`    — supporting code (Python, Bash, etc.) the skill calls or references
- `references/` — long-form reference material (checklists, specs) loaded on demand

Body structure of `SKILL.md`:
- `# <Skill name>` — H1 title
- `## When to use` — concrete triggers, examples in user's language if domain-specific
- `## How to use` — minimal example + API reference
- `## Dependencies` — pip/system packages needed
- `## License` — one-line pointer to the `LICENSE` file
- `## Checklist` (optional) — final verification steps before delivering output

Skills are installed as folders to `~/.claude/skills/<name>/` and discovered by Claude through their description. Don't add new top-level frontmatter fields — Claude Code ignores them silently.

## Conventions

- Commit messages: conventional commits (`feat:`, `fix:`, `docs:`, `chore:`)
- Branch strategy for this repo: main only. (Commands like /sprint auto-detect the repository's default branch.)
- Agents and commands are .md files. Skills are folders with SKILL.md plus optional scripts/references. No code generation, no templates.
- Installer must stay POSIX-safe: `set -euo pipefail`, no bashisms beyond what install.sh already uses.
- Installer must keep both `install.sh` and `install.ps1` in sync: same flags, same `--target` set, same skip-rules per target. When adding a new target, update the `case "$TARGET"` block in install.sh AND the `switch ($Target)` in install.ps1, plus the `[ValidateSet(...)]` attribute and the README target table.
- Arithmetic: use `$((count + 1))` not `((count++))`. The latter exits non-zero on zero under set -e.
- VERSION file: single line, semver, no v prefix. Installers and the release workflow read it at runtime.
- CHANGELOG.md: Keep a Changelog format. Update on every release.
- Agent descriptions are triggers, not summaries. Write for the model to understand WHEN to fire.
- Skill descriptions follow the same trigger principle and may include foreign-language keywords if the skill is domain-specific (see skills/gost-report).
- No @-importing files in agent bodies — bloats every session.
- Model assignment: opus for high-reasoning roles (architect, security). sonnet for everything else.
- Research docs are reference material. Agents do not auto-import them.
- Releases: bump VERSION → update CHANGELOG → commit → `git tag vX.Y.Z` → push tag. The release workflow builds skill zips and attaches them to the GitHub release.

## Do Not

- Add test files or test frameworks. Validation is via real usage, not test harnesses.
- Add CI workflows beyond `.github/workflows/release.yml` (skill packaging) unless the project gains an actual test suite.
- Create a `.claude/` directory in the repo. It is gitignored. The repo IS the source for `~/.claude/`.
- Add new frontmatter fields beyond what is documented above. Claude Code ignores unknown fields silently.
- Change installer flags without updating both install.sh and install.ps1.
- Put implementation details in agent or skill descriptions. Descriptions say WHEN to invoke, not HOW.
- Commit anything under `dist/` — it is generated by `scripts/build-skills.sh` and gitignored.

## Adding a New Agent

1. Create `agents/name.md` with frontmatter: name, description, tools, model
2. Description must contain "MUST BE USED when/before..." trigger language
3. Choose tools by role: read-only agents get no Write/Edit, write agents get full access
4. Choose model: opus only if the role requires deep multi-step reasoning
5. Include Handoff Protocol section with RECOMMEND: lines to related agents
6. Update README.md agents table, docs/agents.md, and CHANGELOG.md
7. Run `./install.sh` to deploy, then test the agent in a real project

## Adding a New Command

1. Create `commands/name.md` with frontmatter: description, argument-hint (if it takes args)
2. Body starts with `## Context` containing `@CLAUDE.md`
3. If it takes arguments: add `$ARGUMENTS` under a descriptive heading
4. Pipeline uses `### Step N: Name` structure with agent prompts in quotes
5. Each step that invokes an agent: "Run the `name` agent:" followed by prompt
6. Update README.md commands table, docs/commands.md, and CHANGELOG.md
7. Run `bash install.sh` to deploy, then test the command in a real project

## Adding a New Skill

1. Create `skills/<name>/SKILL.md` with frontmatter: `name` (must equal the folder name) and `description` (trigger sentence)
2. Body explains *when* the skill applies, then *how* to use it (minimal example + API reference)
3. Add `skills/<name>/LICENSE` (MIT by default — copy from `skills/gost-report/LICENSE` and update the year/owner)
4. Optional: `skills/<name>/scripts/` for supporting code, `skills/<name>/references/` for long-form docs
5. Update README.md skills table and CHANGELOG.md
6. Run `bash install.sh` to deploy locally, then test by invoking the skill in a real project
7. Run `bash scripts/build-skills.sh` to verify the release archive builds cleanly and includes LICENSE

## Contributing

Fork the repo. Branch from main. PR to main.
Keep agent prompts specific and actionable. Avoid vague instructions.
Every PR must update relevant docs (README, docs/, CHANGELOG.md).
