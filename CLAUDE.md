# claude-agents

Multi-agent orchestration for Claude Code. 8 agents, 15 commands, 10 research docs.
Installed globally to ~/.claude/ — not a runtime application. No build, no tests, no dependencies.

## Commands

```
./install.sh                    # install agents + commands to ~/.claude/
./install.sh --dry              # preview what would change
./install.sh --diff             # show repo vs installed differences
./install.sh --pull             # copy installed back to repo
```

## Structure

```
agents/*.md         Agent definitions — YAML frontmatter + system prompt
commands/*.md       Slash commands — YAML frontmatter + orchestration pipeline
research/*.md       Reference docs, numbered 01-10. Not auto-imported by agents.
docs/               User-facing documentation (commands.md, agents.md, installation.md)
install.sh          Bash installer (macOS, Linux, WSL, Git Bash)
install.ps1         PowerShell installer (Windows)
VERSION             Semver string, read by installers
CHANGELOG.md        Keep a Changelog format
```

## Agent Format (agents/*.md)

YAML frontmatter then markdown body. Exact fields:

```
name:         lowercase identifier (architect, dba, devops, docs, pm, reviewer, security, tester)
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

## Conventions

- Commit messages: conventional commits (`feat:`, `fix:`, `docs:`, `chore:`)
- Branch strategy for this repo: main only. (Commands like /sprint auto-detect the repository's default branch.)
- All agents and commands are .md files. No code generation, no templates.
- Installer must stay POSIX-safe: `set -euo pipefail`, no bashisms beyond what install.sh already uses.
- Arithmetic: use `$((count + 1))` not `((count++))`. The latter exits non-zero on zero under set -e.
- VERSION file: single line, semver, no v prefix. Installers read it at runtime.
- CHANGELOG.md: Keep a Changelog format. Update on every release.
- Agent descriptions are triggers, not summaries. Write for the model to understand WHEN to fire.
- No @-importing files in agent bodies — bloats every session.
- Model assignment: opus for high-reasoning roles (architect, security). sonnet for everything else.
- Research docs are reference material. Agents do not auto-import them.

## Do Not

- Add test files or test frameworks. Validation is via real usage, not test harnesses.
- Add CI workflows. This project has no build step and no tests to run.
- Create a `.claude/` directory in the repo. It is gitignored. The repo IS the source for `~/.claude/`.
- Add new frontmatter fields beyond what is documented above. Claude Code ignores unknown fields silently.
- Change installer flags without updating both install.sh and install.ps1.
- Put implementation details in agent descriptions. Descriptions say WHEN to invoke, not HOW the agent works.

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
7. Run `./install.sh` to deploy, then test the command in a real project

## Contributing

Fork the repo. Branch from main. PR to main.
Keep agent prompts specific and actionable. Avoid vague instructions.
Every PR must update relevant docs (README, docs/, CHANGELOG.md).
