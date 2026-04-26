# agentpipe

Gated pipeline orchestration for **Claude Code** and **Codex CLI** — specialist agents, slash commands, and skills installed globally. Unlike "config sync" tools, agentpipe gives you ready-made multi-agent workflows: `/feature` runs `pm → architect → reviewer → security → tester → docs` as a gated pipeline, `/sprint` works through GitHub issues one at a time, `/audit` runs threat modeling. Multi-vendor: same skills work in both Claude and Codex.

## What This Is

Three kinds of installable extensions, plus reference material:

- **Agents** — specialist personas (architect, DBA, security engineer, etc.) with bounded tool access
- **Commands** — orchestration pipelines (`/feature`, `/sprint`, `/deploy`, etc.) that coordinate agents through gated workflows
- **Skills** — domain capabilities Claude invokes automatically (e.g. generating ITMO/GOST academic reports as `.docx`)
- **Research** — reference documents on security, testing, observability, and API design

## Quick Start

```bash
git clone https://github.com/zevtos/agentpipe.git
cd agentpipe

# Claude Code (default target) — installs agents + commands + skills to ~/.claude/
bash install.sh

# Codex CLI — installs skills only to ~/.agents/skills/
bash install.sh --target codex

# Windows PowerShell
.\install.ps1                   # Claude Code
.\install.ps1 -Target codex     # Codex CLI
```

For Claude Code, then use commands like `/next`, `/feature`, `/review`, or just describe a task that matches a skill (e.g. «сделай лабораторную ИТМО по теме X»). For Codex, invoke skills via `$gost-report` (or let Codex pick by description). See [Codex support](#codex-support) below.

## Commands

| Command | Purpose | Agents Used |
|---------|---------|-------------|
| `/next` | What should I work on? Strategic project analysis | architect |
| `/kickoff` | Bootstrap a new project from an idea | pm + architect + dba + devops |
| `/plan` | Architecture planning (design only, no code) | architect |
| `/feature` | End-to-end feature development | pm + architect + reviewer + security + tester + docs |
| `/fix` | Bug investigation and fix | reviewer + tester |
| `/sprint` | Work through GitHub issues one by one | architect + reviewer + security + tester + dba* |
| `/refactor` | Safe refactoring with verification gates | refactorer + architect* + reviewer + tester* |
| `/review` | Multi-agent code review | reviewer + security (parallel) |
| `/test` | Test strategy, implementation, mutation testing | tester |
| `/audit` | Full security audit with threat modeling | security + architect |
| `/deploy` | Deployment pre-flight checks and readiness gate | devops + reviewer + docs |
| `/release` | Version bump, changelog, tag | docs |
| `/db` | Safe database schema migrations | dba + reviewer + tester |
| `/docs` | Generate/update project documentation | docs |
| `/onboard` | Understand a new codebase | (exploration only) |

## Agents

| Agent | Domain | Model |
|-------|--------|-------|
| architect | System design, API contracts, ADRs | opus |
| pm | Requirements, specs, acceptance criteria | sonnet |
| dba | Schema design, migrations, query optimization | sonnet |
| devops | CI/CD, Docker, observability, deployment | sonnet |
| reviewer | Code review, quality gates | sonnet |
| security | Threat modeling, OWASP, vulnerability audit | opus |
| tester | Test strategy and implementation | sonnet |
| refactorer | Code smell detection, duplication, test quality | sonnet |
| docs | API docs, ADRs, runbooks, changelogs | sonnet |

## Skills

| Skill | Purpose | Triggers on |
|-------|---------|-------------|
| `gost-report` | Generate Russian academic reports (`.docx`) formatted to GOST 7.32 — лабораторные, отчёты по практике, курсовые, ВКР. Two built-in profiles (`ITMO_PROFILE`, `GOST_PROFILE`) plus `UniversityProfile` for any other vuz. | Russian-language asks for «лабораторную», «отчёт по ГОСТ», «курсовую», «ИТМО», etc. |

Each release attaches every skill as a standalone `.zip` to the GitHub release page. Two install paths depending on where you use Claude:

- **Claude Code (CLI)** — `unzip gost-report.zip -d ~/.claude/skills/` (or `bash install.sh` from a clone of the repo)
- **Claude Chat (claude.ai)** — open any chat, go to **Customize → Skills → Add → Create skill → Upload a skill**, and pick the zip. The skill becomes globally available across all your conversations.

Adding your university? Build a `UniversityProfile` and PR it back.

## Project Structure

```
agentpipe/
  agents/                9 specialist agent definitions
  commands/              15 orchestration commands
  skills/                Domain-specific Claude skills (folders with SKILL.md + assets)
  research/              14 reference documents
  scripts/               build-skills.sh / .ps1 — package skills into release zips
  .github/workflows/     release.yml — auto-attaches skill zips to GH releases on tag push
  docs/                  documentation (commands.md, agents.md, installation.md)
  install.sh             bash installer (macOS/Linux/WSL)
  install.ps1            PowerShell installer (Windows)
```

## Documentation

- [Commands Reference](docs/commands.md) — detailed usage for each command
- [Agents Reference](docs/agents.md) — agent capabilities and responsibilities
- [Installation Guide](docs/installation.md) — install, update, and customize

## How It Works

Commands are gated pipelines. For example, `/feature` runs:

```
PM agent (spec) -> Architect agent (design) -> Implement -> Reviewer agent -> Tester agent -> Docs agent
```

Each gate must pass before proceeding. Agents have bounded tool access (reviewer can read and grep but not write). Critical agents (architect, security) run on opus; others run on sonnet. Agents marked with * in the table above are conditional (e.g., dba runs only when schema changes are detected).

## Codex Support

Codex CLI shares the open-agent-skills format with Claude, so the same `gost-report` skill folder works in both. The installer's `--target codex` flag puts skills in `~/.agents/skills/` (Codex's standard path — note: not `~/.codex/skills/`).

What gets installed per target:

| | Claude Code (`--target claude`, default) | Codex CLI (`--target codex`) |
|---|---|---|
| Skills (`skills/*/`) | `~/.claude/skills/` | `~/.agents/skills/` |
| Agents (`agents/*.md`) | `~/.claude/agents/` | skipped (Codex agents use TOML, different format) |
| Slash commands (`commands/*.md`) | `~/.claude/commands/` | skipped (Codex CLI has no custom slash commands) |

If you use both clients, run the installer twice — once per target. The two install paths don't conflict.

**Why are agents and commands skipped for Codex?**

- Codex agents are single TOML files in `~/.codex/agents/` with fields like `developer_instructions`, `model`, `sandbox_mode` — not Markdown with YAML frontmatter. Auto-translating Claude's prompts to that format is a planned future feature; for now, treat the agent files as reference and write equivalent TOML by hand if needed.
- Codex CLI has no analogue to Claude's custom slash commands — Codex equivalent of «my workflow» is a skill. The user invokes it via `$skill-name`. Many of this repo's commands (`/next`, `/feature`, etc.) could become Codex skills in a future release.

## Customization

- **Project-level overrides**:
  - Claude: `.claude/agents/`, `.claude/commands/`, `.claude/skills/` in your project
  - Codex: `.agents/skills/` in your project (or `.codex/agents/` for TOML agents)
- **Edit globals**: modify the installed files directly under the target directory
- **Fork**: fork this repo, customize, install from your fork

## Building Release Archives

```bash
bash scripts/build-skills.sh           # zips every skills/<name>/ into dist/<name>.zip
bash scripts/build-skills.sh itmo-report   # build a single skill
```

On `git push --tags vX.Y.Z`, the `release.yml` workflow runs the same script and attaches the zips to the GitHub release. Tag must match `VERSION` exactly.

## Contributing

Contributions welcome — new agents, commands, and improvements to existing ones. Open an issue or submit a PR.

## License

[MIT](LICENSE)

## Research Documents

The `research/` directory contains reference material used by agents:

| File | Topic |
|------|-------|
| 01 | Claude Code agent patterns guide |
| 02 | Blockchain wallet security audit standards |
| 03 | Incident response and postmortems |
| 04 | Production readiness checklist (SRE) |
| 05 | Advanced testing strategies |
| 06 | DevOps and observability |
| 07 | Database engineering patterns |
| 08 | API and system design patterns |
| 09 | Mobile production best practices |
| 10 | Documentation systems for AI agents |
| 11 | Code smell detection and refactoring taxonomy |
| 12 | Test refactoring and quality patterns |
| 13 | Building an AI refactoring agent |
| 14 | Architecture-level refactoring |
