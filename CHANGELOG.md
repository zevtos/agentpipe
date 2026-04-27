# Changelog

All notable changes to this project will be documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-04-27

### Added
- **`gost-report`: native LaTeX equations.** New API method `r.formula(latex, where=None) → int` inserts a LaTeX formula as a real Word equation (OMML), not a rendered image — the formula opens editable in Word, scales cleanly, and looks native. Supports the full `latex2mathml` subset: powers/subscripts, `\frac`, `\sqrt[n]`, Greek letters, `\sum/\int/\prod` with limits, `\bar/\hat/\vec/\tilde/\dot` accents, `pmatrix/bmatrix/vmatrix`, `\text{...}`, fenced groups. Auto-numbered «(N)» pinned to the right edge per GOST 7.32, optional `where=...` block for variable explanations renders below the formula. Returns the formula's number so prose can reference it: `f1 = r.formula(...)` → `r.text(f"По формуле ({f1}) ...")`. The LaTeX string itself bypasses `_sanitize_prose` (so `\text{1—5}` doesn't get mangled); `where` text is sanitized as normal user prose.
- **`gost-report`: isolated Python environment.** New `scripts/ensure_env.py` bootstrap manages the skill's deps in a dedicated venv at `<skill_dir>/.venv/` — fixed location, single per installation, never global. Manager preference: `uv` → `conda --prefix` → stdlib `python -m venv`. Idempotent: hashes `requirements.txt` and skips work when nothing changed (millisecond no-op on warm runs). When the skill ships a new `requirements.txt`, deps auto-update on next invocation. Two modes: `python ensure_env.py` (prints venv python path) and `python ensure_env.py <script>` (bootstraps then `os.execv`s the script with venv python). Cross-platform (POSIX + Windows), stdlib only, with best-effort flock to prevent concurrent-bootstrap races.
- **`gost-report`: `scripts/requirements.txt`** — `python-docx>=1.1.0`, `latex2mathml>=3.77.0`. Single source of truth for both pip and the bootstrap.

### Changed
- **`gost-report` SKILL.md** — `## Dependencies` rewritten: agents are now told to **never** `pip install` globally, always go through `ensure_env.py`. New `### Формулы (LaTeX)` section with examples and supported subset. New `### Запуск через ensure_env.py` block under `## How to use` documenting the bootstrap workflow. Checklist gains two items (formulas render as equations, single venv per skill).
- **Installers** (`install.sh`, `install.ps1`) and **build script** (`scripts/build-skills.sh`) — strip `.venv/` and `.venv.lock` on copy/zip in both directions (install + pull). Prevents a developer's local venv from leaking into system installations or release archives, where the absolute paths in `pyvenv.cfg` would be wrong anyway.
- **`.gitignore`** — `skills/*/.venv/` and `skills/*/.venv.lock` so `git status` stays clean during local skill development.

## [0.4.4] - 2026-04-27

### Added
- **`gost-report`: hard enforcement against em-dashes.** The soft writing-style rule from v0.4.3 was still being ignored — agents kept putting `—` into report prose. Now two layers of protection:
  1. **Prompt.** `SKILL.md` opens with a top-level "⛔ Правило №1 — приоритет выше всего остального" block right after the H1, listing exactly where em/en-dashes are forbidden (`r.text`, `r.task`, headings, list items, captions, table cells) and what to write instead (comma between words, hyphen in ranges). The old writing-style section now defers to this rule.
  2. **Code.** `gost_report.py` adds `_sanitize_prose()` — every content method (`text`, `task`, `h1/h2/h3`, `numbered`, `bullet`, table cells, and the user `caption` argument in `figure`/`table`) runs input through regexes: ` — ` / ` – ` → `, ` and bare `—`/`–` → `-`. The library-generated prefixes «Рисунок N — Описание» and «Таблица N — Описание» are untouched because the dash there is mandated by GOST and inserted by the library itself.
- The sanitizer is a safety net, not a license to relax — `SKILL.md` explicitly tells the model not to rely on it and to write without em-dashes from the start.

## [0.4.3] - 2026-04-26

### Added
- **`gost-report`: hard writing-style rule for prose.** New `## Writing style — обязательно соблюдать` section in `SKILL.md` makes the model write report body text in a natural Russian voice instead of the typical AI register: no em-dashes, no «ёлочки», no канцеляризмы («в ходе выполнения работы», «является», «данный», etc.), varied sentence rhythm. Scoped to prose only — auto-generated captions (`Рисунок N — Описание`) and canonical structural headings (`Введение`, `Заключение`) are explicitly exempt because they're library-formatted per GOST.

## [0.4.2] - 2026-04-26

### Fixed
- **`gost-report`: large screenshots no longer overflow page margins.** `figure()` now clamps the image width to the printable area (A4 width minus left+right body margins from the active profile — typically ~17 cm for ITMO, ~16.5 cm for GOST). Previously, calling `figure(path, caption)` without `width_cm` used the image's native dimensions, which for typical screenshots (1300-2500 px @ 144 DPI = 23-43 cm) extended past the right margin and into the page edge in Word. Verified end-to-end on real lab screenshots (Cisco Packet Tracer captures); images now fit cleanly. Explicitly set `width_cm` is also clamped as a safety net.
- No PIL/Pillow dependency added — image natural size is read via `docx.image.image.Image.from_file`, which python-docx already ships.

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
