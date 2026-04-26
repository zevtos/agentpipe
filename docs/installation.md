# Installation

## Prerequisites

- [Claude Code](https://claude.ai/code) installed and configured
- Git (for cloning the repository)

## Quick Install

### macOS / Linux / WSL

```bash
git clone https://github.com/zevtos/claude-agents.git claude-agents
cd claude-agents
bash install.sh
```

### Windows (PowerShell)

```powershell
git clone https://github.com/zevtos/claude-agents.git claude-agents
cd claude-agents
.\install.ps1
```

### What Gets Installed

The installer copies agents, commands, and skills to your Claude Code config directory:

```
~/.claude/
  agents/                 <- 9 specialist agents (.md files)
  commands/               <- 15 orchestration commands (.md files)
  skills/                 <- skill folders (each contains SKILL.md + optional scripts/references)
    itmo-report/
```

On WSL, files are installed to the Windows-side Claude config directory automatically.

### Installing a Skill from a Release Zip

If you don't want to clone the repo, every release attaches each skill as `<skill>.zip` to the GitHub release page. Extract directly into your skills directory:

```bash
unzip ~/Downloads/itmo-report.zip -d ~/.claude/skills/
```

The zip already contains the `<skill>/` folder at the top level, so it lands at `~/.claude/skills/<skill>/`.

## Installer Options

| Flag | Bash | PowerShell | Description |
|------|------|------------|-------------|
| Dry run | `--dry` | `-Dry` | Show what would be copied without copying |
| Diff | `--diff` | `-Diff` | Show differences between repo and installed files |
| Pull | `--pull` | `-Pull` | Copy installed files back to repo (reverse sync) |
| Help | `--help` | `-Help` | Show usage information |

## Updating

Pull the latest changes and re-run the installer:

```bash
cd claude-agents
git pull
bash install.sh
```

The installer overwrites existing files — your installed agents and commands will match the repo.

## Verify Installation

After installing, open Claude Code in any project and try:

```
/onboard
```

If you see the onboarding pipeline start, installation was successful.

## Customization

After installation, you can customize agents, commands, and skills for your specific project:

- **Project-level overrides**: Create `.claude/agents/`, `.claude/commands/`, or `.claude/skills/` in your project directory. Project-level files take precedence over global ones.
- **Edit installed files**: Modify files in `~/.claude/agents/`, `~/.claude/commands/`, `~/.claude/skills/` directly. Note: re-running the installer will overwrite your changes.
- **Fork and modify**: Fork this repo, customize, and install from your fork.

## Building Release Archives

Maintainers / forks who want to publish skills as standalone downloads:

```bash
bash scripts/build-skills.sh             # build dist/<skill>.zip for every skill
bash scripts/build-skills.sh itmo-report # build a single skill
```

Output lands in `dist/` (gitignored). On `git push --tags vX.Y.Z`, the `release.yml` workflow runs the same script and attaches the resulting zips to the GitHub release. The workflow refuses to publish if the tag does not match `VERSION`.
