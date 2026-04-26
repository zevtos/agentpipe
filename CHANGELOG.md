# Changelog

All notable changes to this project will be documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.1] - 2026-04-26

### Changed
- **Repo renamed** `claude-agents` → `agentpipe`. New URL: https://github.com/zevtos/agentpipe. GitHub redirects the old URL automatically, so existing clones / links / `gh` commands keep working; for cleanliness run `git remote set-url origin git@github.com:zevtos/agentpipe.git`.
- README repositioned: "gated pipeline orchestration for Claude Code and Codex CLI" instead of generic "config" — the orchestration angle (`/feature` runs `pm → architect → reviewer → security → tester → docs`) is what differentiates this project from the saturated "AI config sync" namespace.
- Project identity strings in installers, build scripts, and `CLAUDE.md` updated from `claude-agents` to `agentpipe`. Past CHANGELOG entries kept verbatim for historical accuracy.

## [0.4.0] - 2026-04-26

### Added
- **Codex CLI support** — installer accepts `--target codex` (Bash) / `-Target codex` (PowerShell). Skills go to `~/.agents/skills/` (open-agent-skills standard, NOT `~/.codex/skills/`). Default target is still `claude` so existing workflows are unaffected.
- All installer actions (`install`, `--dry`, `--diff`, `--pull`, `--uninstall`) respect `--target`.
- README has a new "Codex Support" section with the per-target install matrix and rationale for skipping agents/commands in Codex mode.
- `CLAUDE.md` documents the multi-target architecture and the rule to keep both installers in sync.

### Changed
- `install.sh` rewritten with proper argument-loop parsing instead of single-flag dispatch.
- `install.ps1` rewritten with `-Target` parameter, `[ValidateSet("claude","codex")]` validation, and full skills support (the latter was missing in v0.3.x — bug fix).

### Notes
- Codex agents (TOML format in `~/.codex/agents/`) and Codex's lack of custom slash commands mean those Claude assets are intentionally skipped under `--target codex`. Auto-translating agents to TOML is a planned future feature.

## [0.3.1] - 2026-04-26

### Added
- **`skills/gost-report/LICENSE`** (MIT) — every skill now ships its own license so the standalone `.zip` is legally distributable when uploaded to Claude Chat or extracted into `~/.claude/skills/`. Same MIT terms as the repo top-level.
- `CLAUDE.md` updated: skill folders must include `LICENSE`; documented in `Skill Format` and `Adding a New Skill`.
- `SKILL.md` for `gost-report` now has a `## License` section.

## [0.3.0] - 2026-04-26

### Added
- **Skills support** — installer now copies `skills/<name>/` directories to `~/.claude/skills/<name>/` alongside agents and commands
- **`gost-report` skill** — generates Russian academic reports (`.docx`) formatted to GOST 7.32 for any vuz: лабораторные, отчёты по практике, курсовые, ВКР. Ships two profiles (`ITMO_PROFILE` as default, `GOST_PROFILE` as universal baseline) and a `UniversityProfile` dataclass for arbitrary universities (МГУ, СПбГУ, МФТИ, Бауманка, etc.)
- **`scripts/build-skills.sh`** (and `.ps1`) — packages every `skills/<name>/` into `dist/<name>.zip`
- **Release workflow** (`.github/workflows/release.yml`) — on `vX.Y.Z` tag push, builds skill zips and attaches them to the GitHub release; verifies tag matches `VERSION`
- `dist/`, `__pycache__/`, `*.pyc` added to `.gitignore`

### Changed
- Project framing: from "multi-agent orchestration" to "comprehensive Claude Code configuration" (agents + commands + skills)
- `install.sh` and `install.ps1` now also handle skills and report a unified item count
- `CLAUDE.md` documents skill format, the release flow, and the `Adding a New Skill` checklist
- All examples use placeholder names («Фамилия И.О.») — never real ones

## [0.2.0] - 2026-04-13

### Added
- **refactorer agent** — 9th specialist agent for code smell detection, duplication elimination, dead code removal, and test suite refactoring (language-agnostic)
- 4 new research docs: code smells catalog, test patterns, AI-assisted refactoring, architecture debt
- `/refactor` command now runs refactorer as mandatory first step before architect
- `CLAUDE.md` with project conventions, agent/command format specs, and contributor guide — the repo now dogfoods its own pattern
- MIT LICENSE file

### Fixed
- `/sprint` no longer hardcodes `develop` as the base branch; auto-detects the repository's default branch with fallback to `main`
- Repo URLs in README replaced with actual GitHub URL

## [0.1.0] - 2026-04-04

Initial release of claude-agents — multi-agent orchestration for Claude Code.

### Added
- 8 specialist agents: architect, pm, dba, devops, reviewer, security, tester, docs
- 15 orchestration commands: /next, /kickoff, /plan, /onboard, /feature, /fix, /refactor, /sprint, /review, /test, /audit, /deploy, /release, /db, /docs
- 10 research reference documents (security, testing, DevOps, API design, databases, mobile, AI docs)
- Bash installer for macOS, Linux, WSL, Git Bash (`install.sh`)
- PowerShell installer for Windows (`install.ps1`)
- Installer modes: `--dry`, `--diff`, `--pull`, `--uninstall`, `--version`
- Complete documentation: README, commands guide, agents guide, installation guide

### Fixed
- Shell arithmetic compatibility with `set -e` in installer (`((count++))` → `$((count + 1))`)
- Architect agent made mandatory in /plan command pipeline
