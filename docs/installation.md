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
| Update | `--update` | `-Update` | `git pull --ff-only` then install (one-shot upgrade) |
| Uninstall | `--uninstall` | `-Uninstall` | Remove installed files (respects `--target`) |
| No attribution-fix | `--no-attribution-fix` | `-NoAttributionFix` | Skip Co-Authored-By suppression layer (see below) |
| No config-defaults | `--no-config-defaults` | `-NoConfigDefaults` | Skip $schema URL + secret-file deny-list (see below) |
| Help | `--help` | `-Help` | Show usage information |

All actions respect `--target`. Examples:

```bash
bash install.sh --target codex --dry        # preview Codex install
bash install.sh --target codex --uninstall  # remove only Codex skills
bash install.sh --diff                      # diff Claude install (default target)
```

## Safe Defaults Layer

By default, on `--target claude`, the installer also adds two universal defaults to `~/.claude/settings.json`:

1. **`$schema`** — points at [`json.schemastore.org/claude-code-settings.json`](https://json.schemastore.org/claude-code-settings.json), so VS Code, Cursor, etc. give autocomplete and inline validation when you edit the file.
2. **`permissions.deny`** — set-union (preserves your existing entries) of the universally-unsafe file-read patterns:
   - `Read(./.env)`
   - `Read(./.env.*)`
   - `Read(./**/secrets/**)`
   - `Read(./**/*.pem)`
   - `Read(./**/*.key)`

The deny list is intentional: these are paths nobody wants Claude reading regardless of stack. We deliberately do **not** ship a `permissions.allow` list — that's stack-specific (a Python user doesn't want `Bash(npm run *)` pre-allowed). Use Claude Code's built-in `fewer-permission-prompts` skill to build an allow-list dynamically from your actual usage.

Pass `--no-config-defaults` (Bash) or `-NoConfigDefaults` (PowerShell) to skip this layer. `--uninstall` does **not** auto-remove these keys (preserves user state); edit `settings.json` manually to revert.

## Removing Claude Attribution from Commits

By default, on `--target claude`, the installer suppresses the `Co-Authored-By: Claude <noreply@anthropic.com>` and `🤖 Generated with [Claude Code]` trailers Claude Code adds to commit messages. Two layers, both idempotent:

1. **Settings keys.** Writes the modern `attribution: { commit: "", pr: "" }` (the official, current way to hide attribution) **and** the legacy `includeCoAuthoredBy: false` (kept for backward compat with older Claude Code that doesn't read `attribution`). Both keys are deep-merged into `~/.claude/settings.json` — existing user keys are preserved.
2. **Global git `commit-msg` hook.** Copies `scripts/git-hooks/commit-msg` to `~/.git-templates/hooks/commit-msg` and points `git config --global init.templateDir` at `~/.git-templates`. The hook strips both trailers (including model-named variants like `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`) and any resulting blank lines from every commit message, so anything that slips past the settings keys still gets cleaned up.

**Opt out:** pass `--no-attribution-fix` (Bash) or `-NoAttributionFix` (PowerShell). The flag has no effect on `--target codex`.

**Existing repos.** New repos created via `git init` after the installer runs inherit the hook automatically. **Existing repos are not modified** — `git init` inside an already-initialized repo does not refresh hooks. To apply the hook to an existing repo, either run `git init` inside it (a no-op for everything except hooks; safe), or copy `~/.git-templates/hooks/commit-msg` into `.git/hooks/` manually.

**Existing template dir.** If `git config --global init.templateDir` is already set to a non-default path, the installer refuses to override it and prints a notice. Copy the hook into your existing template dir's `hooks/` folder manually.

**Existing `commit-msg` hook.** If `~/.git-templates/hooks/commit-msg` already exists with different content, it's backed up to `commit-msg.agentpipe.bak.<epoch>` before being replaced. The installer warns loudly when this happens.

**Uninstall behavior.** `--uninstall` removes the hook file and unsets `init.templateDir` (only if it points to our path). It does **not** auto-remove `includeCoAuthoredBy` from `settings.json` — that key remains until you edit the file manually. The installer prints a reminder.

### Cleaning already-committed trailers

The installer only prevents future trailers; it does not rewrite commits already in your history. If you have local or pushed commits with the trailers and want to scrub them, use [`git-filter-repo`](https://github.com/newren/git-filter-repo):

```bash
# Inside the repo. Requires git-filter-repo (pip install git-filter-repo).
git filter-repo --message-callback '
  return re.sub(rb"\n*🤖 Generated with \[Claude Code\][^\n]*\nCo-Authored-By: Claude <noreply@anthropic\.com>\n?", b"", message)
'
git push --force-with-lease
```

> **Warning:** this rewrites commit history. Force-pushing breaks collaborators' clones — coordinate with everyone who has pulled the affected branches before running it. Don't use this on shared `main` without explicit consensus.

## Updating

One-shot upgrade (pulls latest from remote, then installs):

```bash
cd agentpipe
bash install.sh --update     # bash on macOS/Linux/WSL/Git Bash
.\install.ps1 -Update        # PowerShell on Windows
```

`--update` runs `git pull --ff-only` first; if your working tree has uncommitted changes or the remote has diverged, it stops with a clear error so nothing is clobbered. Equivalent to `git pull && bash install.sh` when both succeed.

If you prefer the manual flow:

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

## After Install: Terminal & Keybindings

If your terminal emulator needs Shift+Enter to insert a newline (most do — VS Code, Cursor, Windsurf, Alacritty, Zed), run **`/terminal-setup`** once inside a Claude Code session in that terminal. Native-supported terminals (iTerm2, WezTerm, Ghostty, Kitty, Warp, Apple Terminal) don't need it. Run it in the host terminal — not inside `tmux` / `screen`.

Newline fallbacks that work in any terminal: **`Ctrl+J`** or **`\` + Enter`**.

Useful keybindings:

| Key | Action |
|---|---|
| `Ctrl+C` | Cancel the current generation/input |
| `Ctrl+D` | Exit the session |
| `Esc` | Interrupt mid-response (community-known; `Ctrl+C` is the documented stop) |
| `Esc` `Esc` | Open the rewind menu — three modes: conversation, code, both |
| `Ctrl+V` | Paste an image from clipboard (`Cmd+V` in iTerm2, `Alt+V` on Windows) |
| `Shift+Tab` | Cycle permission modes: default → acceptEdits → plan |
| `Ctrl+T` | Show task list |
| `Ctrl+O` | Toggle verbose/transcript view (helps debug agent quality) |

Reference: [Interactive mode docs](https://code.claude.com/docs/en/interactive-mode), [terminal config docs](https://code.claude.com/docs/en/terminal-config).

## Optional: Shell Environment Variables

agentpipe ships `scripts/agentpipe.env.example` with a curated set of Claude Code env vars (reasoning effort, adaptive-thinking override, telemetry bundle) and explanations of trade-offs. **Not auto-installed and not auto-sourced** — shell rc is the user's territory; pick what fits.

To use: copy what you want into `~/.zshrc` / `~/.bashrc`, or copy the whole file to `~/.claude/agentpipe.env` and add this line to your rc:

```bash
[ -f ~/.claude/agentpipe.env ] && source ~/.claude/agentpipe.env
```

Caveat: `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING` is a no-op on Opus 4.7 (always adaptive at the API level). `CLAUDE_CODE_EFFORT_LEVEL=max` removes the per-turn token cap — quality up, paid-plan quota burns faster.

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
