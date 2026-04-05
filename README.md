# Claude Agents

Multi-agent orchestration system for Claude Code. Transforms a single AI assistant into a coordinated development team with 8 specialist agents and 15 pipeline commands.

## What This Is

A set of installable agents and commands that add structured development workflows to Claude Code:

- **Agents** — specialist personas (architect, DBA, security engineer, etc.) with bounded tool access
- **Commands** — orchestration pipelines (`/feature`, `/sprint`, `/deploy`, etc.) that coordinate agents through gated workflows
- **Research** — reference documents on security, testing, observability, and API design

## Quick Start

```bash
git clone https://github.com/zevtos/claude-agents.git claude-agents
cd claude-agents
./install.sh        # macOS/Linux/WSL
# or
.\install.ps1       # Windows PowerShell
```

Then open Claude Code in any project and use commands like `/next`, `/feature`, `/review`.

## Commands

| Command | Purpose | Agents Used |
|---------|---------|-------------|
| `/next` | What should I work on? Strategic project analysis | architect |
| `/kickoff` | Bootstrap a new project from an idea | pm + architect + dba + devops |
| `/plan` | Architecture planning (design only, no code) | architect |
| `/feature` | End-to-end feature development | pm + architect + reviewer + security + tester + docs |
| `/fix` | Bug investigation and fix | reviewer + tester |
| `/sprint` | Work through GitHub issues one by one | architect + reviewer + security + tester + dba* |
| `/refactor` | Safe refactoring with verification gates | architect + reviewer + tester* |
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
| docs | API docs, ADRs, runbooks, changelogs | sonnet |

## Project Structure

```
claude-agents/
  agents/           8 specialist agent definitions
  commands/         15 orchestration commands
  research/         10 reference documents
  docs/             documentation
    commands.md     detailed command reference
    agents.md       detailed agent reference
    installation.md install guide and customization
  install.sh        bash installer (macOS/Linux/WSL)
  install.ps1       PowerShell installer (Windows)
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

## Customization

- **Project-level overrides**: add `.claude/agents/` or `.claude/commands/` in your project
- **Edit globals**: modify `~/.claude/agents/` and `~/.claude/commands/` directly
- **Fork**: fork this repo, customize, install from your fork

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
