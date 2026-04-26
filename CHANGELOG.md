# Changelog

All notable changes to this project will be documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
