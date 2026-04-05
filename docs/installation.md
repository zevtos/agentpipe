# Installation

## Prerequisites

- [Claude Code](https://claude.ai/code) installed and configured
- Git (for cloning the repository)

## Quick Install

### macOS / Linux / WSL

```bash
git clone https://github.com/zevtos/claude-agents.git claude-agents
cd claude-agents
./install.sh
```

### Windows (PowerShell)

```powershell
git clone https://github.com/zevtos/claude-agents.git claude-agents
cd claude-agents
.\install.ps1
```

### What Gets Installed

The installer copies agent and command files to your Claude Code config directory:

```
~/.claude/
  agents/       <- 8 specialist agents
  commands/     <- 15 orchestration commands
```

On WSL, files are installed to the Windows-side Claude config directory automatically.

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
./install.sh
```

The installer overwrites existing files — your installed agents and commands will match the repo.

## Verify Installation

After installing, open Claude Code in any project and try:

```
/onboard
```

If you see the onboarding pipeline start, installation was successful.

## Customization

After installation, you can customize agents and commands for your specific project:

- **Project-level overrides**: Create `.claude/agents/` or `.claude/commands/` in your project directory. Project-level files take precedence over global ones.
- **Edit installed files**: Modify files in `~/.claude/agents/` and `~/.claude/commands/` directly. Note: re-running the installer will overwrite your changes.
- **Fork and modify**: Fork this repo, customize, and install from your fork.
