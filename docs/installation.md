# Installation

## Prerequisites

- [Claude Code](https://claude.ai/code) installed and configured
- Git (for cloning the repository)

## Quick Install

### macOS / Linux / WSL

```bash
git clone https://github.com/zevtos/agentpipe.git
cd agentpipe
bash install.sh
```

### Windows (PowerShell)

```powershell
git clone https://github.com/zevtos/agentpipe.git
cd agentpipe
.\install.ps1
```

### What Gets Installed

The installer supports two targets via `--target`:

**`--target claude`** (default) — copies agents, commands, and skills to your Claude Code config directory:

```
~/.claude/
  agents/                 <- 9 specialist agents (.md files)
  commands/               <- 15 orchestration commands (.md files)
  skills/                 <- skill folders (each contains SKILL.md + optional scripts/references)
    gost-report/
```

**`--target codex`** — copies skills to Codex's open-agent-skills directory:

```
~/.agents/
  skills/                 <- skill folders (same format as Claude)
    gost-report/
```

For Codex, agents and commands are intentionally skipped: Codex agents use a different TOML format (live in `~/.codex/agents/`), and Codex CLI has no custom slash commands. Both will be revisited in future releases.

If you use both clients, run the installer twice — once per target. The install paths don't conflict.

On WSL, files are installed to the Windows-side directory automatically.

### Installing a Skill from a Release Zip

Every release attaches each skill as `<skill>.zip` to the GitHub release page. There are two install paths depending on where you use Claude:

**Claude Code (CLI)** — extract into your skills directory:

```bash
unzip ~/Downloads/gost-report.zip -d ~/.claude/skills/
```

The zip already contains the `<skill>/` folder at the top level, so it lands at `~/.claude/skills/<skill>/`. The full installer (`bash install.sh`) does the same thing for every skill in this repo.

**Claude Chat (claude.ai)** — the CLI path doesn't apply; skills must be uploaded through the web UI:

1. Open any conversation on claude.ai
2. **Customize → Skills → Add → Create skill → Upload a skill**
3. Select the `<skill>.zip` you downloaded from the release page

The skill becomes globally available across all your conversations on that account.

## Installer Options

| Flag | Bash | PowerShell | Description |
|------|------|------------|-------------|
| Target | `--target <name>` | `-Target <name>` | Install for `claude` (default) or `codex` |
| Dry run | `--dry` | `-Dry` | Show what would be copied without copying |
| Diff | `--diff` | `-Diff` | Show differences between repo and installed files |
| Pull | `--pull` | `-Pull` | Copy installed files back to repo (reverse sync) |
| Uninstall | `--uninstall` | `-Uninstall` | Remove installed files (respects `--target`) |
| Help | `--help` | `-Help` | Show usage information |

All actions respect `--target`. Examples:

```bash
bash install.sh --target codex --dry        # preview Codex install
bash install.sh --target codex --uninstall  # remove only Codex skills
bash install.sh --diff                      # diff Claude install (default target)
```

## Updating

Pull the latest changes and re-run the installer:

```bash
cd agentpipe
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
