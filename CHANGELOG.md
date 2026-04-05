# Changelog

All notable changes to this project will be documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
