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

**`--target codex`** — copies skills to Codex's open-agent-skills directory and Codex's native skills directory:

```
~/.agents/
  skills/                 <- skill folders (same format as Claude)
    gost-report/

~/.codex/
  skills/                 <- skill folders loaded by current Codex sessions
    gost-report/
```

For Codex, agents and commands are intentionally skipped: Codex agents use a different TOML format (live in `~/.codex/agents/`), and Codex CLI has no custom slash commands. Both will be revisited in future releases.

If you use both clients, run the installer twice — once per target. The install paths don't conflict.

On WSL, the Claude target still prefers an existing Windows-side `~/.claude` directory. The Codex target uses the current shell's `$HOME` for `~/.agents` and `~/.codex`; run `install.ps1` from Windows if you want a Windows-side Codex install.

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
| No config-defaults | `--no-config-defaults` | `-NoConfigDefaults` | Skip $schema + autoUpdatesChannel + cleanupPeriodDays + spinnerTipsEnabled + permissions.deny |
| No CLAUDE.md | `--no-claude-md` | `-NoClaudeMd` | Skip neutral CLAUDE.md baseline (default: install-if-missing) |
| No gost-validation | `--no-gost-validation` | `-NoGostValidation` | Skip gost-report Stop-hook validator (default-on for claude target) |
| Skills only | `--skills-only` | `-SkillsOnly` | Copy only `skills/*` — skip agents, commands, and every `settings.json`/hook layer. Composes with both targets and with `--dry`/`--diff`/`--pull`/`--uninstall` |
| With sound hooks | `--with-sound-hooks` | `-WithSoundHooks` | Opt-in: Stop sound hook only (one beep when Claude finishes) |
| With notification sound | `--with-notification-sound` | `-WithNotificationSound` | Opt-in: Notification sound hook only (permission/wait-for-input) |
| Clean sound hooks | `--clean-sound-hooks` | `-CleanSoundHooks` | Strip every sound hook (Stop+Notification) from `settings.json` |
| With thinking summaries | `--with-thinking-summaries` | `-WithThinkingSummaries` | Opt-in: `showThinkingSummaries=true` |
| Model profile | `--model-profile <preset>` | `-ModelProfile <preset>` | Per-agent model assignment: `opus`, `sonnet`, `mixed` (default) |
| Help | `--help` | `-Help` | Show usage information |

All actions respect `--target`. Examples:

```bash
bash install.sh --target codex --dry        # preview Codex install
bash install.sh --target codex --uninstall  # remove only Codex skills
bash install.sh --diff                      # diff Claude install (default target)
bash install.sh --skills-only                # update only skills/* under ~/.claude/
bash install.sh --target codex --skills-only # update only skills/* under ~/.agents/
```

## Safe Defaults Layer

By default, on `--target claude`, the installer adds these defaults to `~/.claude/settings.json`:

1. **`$schema`** — points at [`json.schemastore.org/claude-code-settings.json`](https://json.schemastore.org/claude-code-settings.json) so VS Code, Cursor, etc. give autocomplete and inline validation.
2. **`autoUpdatesChannel: "stable"`** — official default is `"latest"` (beta channel). The Feb–Mar 2026 adaptive-thinking regression shipped via `latest`. Stable lags ~1 week and skips versions with major regressions.
3. **`cleanupPeriodDays: 180`** — official default is `30` days, so old session files are deleted at startup. 180 keeps history for users who return to projects after a month or two.
4. **`spinnerTipsEnabled: false`** — the in-spinner tips are noisy after the first session.
5. **`permissions.deny`** — set-union (preserves your existing entries) of universally-unsafe patterns:
   - **Secret files**: `Read(./.env)`, `Read(./.env.*)`, `Read(./**/secrets/**)`, `Read(./**/*.pem)`, `Read(./**/*.key)`
   - **Destructive Bash**: `Bash(rm -rf /*)`, `Bash(rm -rf ~/*)`, `Bash(rm -rf $HOME/*)`, `Bash(mkfs *)`, `Bash(dd * of=/dev/*)`

The deny list is intentional: these are paths nobody wants Claude reading and commands nobody wants Claude executing, regardless of stack. We deliberately do **not** ship a `permissions.allow` list — that's stack-specific. Use Claude Code's built-in `fewer-permission-prompts` skill to build an allow-list dynamically from your actual usage.

Pass `--no-config-defaults` (Bash) or `-NoConfigDefaults` (PowerShell) to skip this layer. `--uninstall` does **not** auto-remove these keys (preserves user state); edit `settings.json` manually to revert.

## Per-Agent Model Profile (`--model-profile`)

Each agent in `agents/*.md` declares the model it uses (`opus` or `sonnet`) in YAML frontmatter. The repo ships a **mixed** default: `architect` and `security` on opus (deep reasoning roles), the other seven on sonnet. The `--model-profile` flag lets you override that at install time:

| Preset | What it does | Cost |
|---|---|---|
| `mixed` (default) | Byte-identical to source — `architect`+`security` on opus, the rest on sonnet | baseline |
| `opus` | Every agent rewritten to opus | ~5× sonnet on heavy sessions |
| `sonnet` | Every agent rewritten to sonnet | cheaper, lower reasoning ceiling |

Source files in `agents/` are **never modified** — the rewrite happens at copy time. Your choice is persisted to `~/.claude/settings.json` under the key `agentpipeModelProfile`, so subsequent installs (including `update.sh`) reuse it without you having to repeat the flag. Pass the flag again to switch.

```bash
bash install.sh --model-profile opus     # one-time switch — remembered
bash update.sh                           # re-uses opus from settings.json
bash install.sh --model-profile mixed    # reset to default
```

`--diff` and `--dry` compare against the rewritten output (so a profile switch shows real drift, not phantom). `--pull` always strips back to canonical mixed defaults — the repo is never contaminated by your installed profile.

The flag has no effect on `--target codex` (Codex doesn't install agents). Persistence is also skipped on codex.

**Escape hatch.** If you want to override every subagent's model from outside the installer, set `CLAUDE_CODE_SUBAGENT_MODEL=opus` in your shell rc. This wins over the agent's frontmatter — but it overrides **all** subagents, including Claude Code's built-in `Plan` and `Explore`. The installer's `--model-profile` is more granular: it only touches agentpipe's nine agents, not the built-ins.

## CLAUDE.md Baseline (install-if-missing)

By default, if `~/.claude/CLAUDE.md` does not exist, the installer copies a **neutral** baseline from `scripts/CLAUDE.md.example`. The baseline covers communication, honesty, scope, and workflow rules — explicitly **stack-agnostic**, no language-specific style opinions. If `~/.claude/CLAUDE.md` already exists, it is **never overwritten**.

Pass `--no-claude-md` (Bash) or `-NoClaudeMd` (PowerShell) to skip the baseline copy. The file is also not modified on subsequent installer runs once it exists — agentpipe treats it as user-owned after first install.

## gost-report Validation Hook (`--no-gost-validation` to opt out)

By default, on `--target claude`, the installer adds a `Stop` hook to `~/.claude/settings.json` that runs the `gost-report` skill's `validate.py` after every model turn. The validator scans the working directory for `*.gost-meta.json` sentinel files (written by `gost_report.Report.save()` next to each generated `.docx`), then validates each described `.docx` against ГОСТ 7.32 deterministic rules — em-dash leakage in body text, missing or non-monotonic figure/table captions, bare LaTeX in prose, placeholder name leaks, and a small bank of AI-tone phrases.

If any hard-fail rule trips, the hook returns `{"decision": "block", "reason": "..."}` JSON, which Claude Code feeds back to the model as a continuation reason. The model then sees the violation list and self-corrects on the next turn — without `SKILL.md` containing any prose about "remember to validate."

**Sentinel scoping** — the hook only validates `.docx` files that have a sibling `.gost-meta.json`. In projects that don't use `gost-report`, the hook is a ~5ms no-op (no sentinels found).

**Crash safety** — the hook always exits 0, even on its own internal error. It cannot break the `Stop` pipeline.

Pass `--no-gost-validation` (Bash) or `-NoGostValidation` (PowerShell) to skip the hook. The validator script itself still ships with the skill regardless of the flag — `r.save()` always runs L1 validation in-library and raises `GostValidationError` on failures. The hook is the deterministic L2 backup that catches cases where the model bypassed `r.save()` (e.g. used `python-docx` directly, or edited the file via `Bash sed`).

Codex target intentionally skips this layer — Codex CLI has no hooks. The validator script still ships in the codex skill `.zip` and remains usable for manual debugging via `python validate.py --check <docx>`.

## Optional: Sound Hooks (`--with-sound-hooks` / `--with-notification-sound`)

Two **independent** opt-in flags, both off by default:

- `--with-sound-hooks` (`-WithSoundHooks`) — installs only the `Stop` sound hook. Fires one beep when Claude finishes a turn. This is the typical "Claude is done" cue most people want.
- `--with-notification-sound` (`-WithNotificationSound`) — installs only the `Notification` sound hook. Fires when Claude is waiting for input or requests a permission.

The OS-appropriate command is auto-detected:

- **macOS**: `afplay /System/Library/Sounds/Hero.aiff` (Stop) + `Glass.aiff` (Notification)
- **Linux**: `paplay /usr/share/sounds/freedesktop/stereo/complete.oga`
- **WSL**: `powershell.exe -c '[console]::beep(800,200)'`
- **Windows (PowerShell installer)**: `[console]::beep(880,150)` (Stop) / `[console]::beep(660,250)` (Notification)

The merge is set-union — your existing hook entries are preserved.

**Both flags together** are allowed but warned. `Notification` often fires immediately after `Stop` (Claude finishes → "waiting for input" notification), so you'd hear two beeps in sequence at the end of each chat. Pass only one flag if that's not what you want.

### Resetting Sound Hooks (`--clean-sound-hooks`)

`--clean-sound-hooks` (`-CleanSoundHooks`) is an **action** (like `--uninstall`) that strips every sound-hook entry from `~/.claude/settings.json`. Useful if:

- You earlier installed both hooks (the previous default, before this flag split) and want to keep only one.
- You want a clean slate before re-adding with the new single-purpose flags.

The command recognises and removes: `afplay` (macOS), `paplay` (Linux), `[console]::beep` (Windows native), `powershell.exe ... beep` (WSL). **Non-sound hooks are preserved** — gost-validation, user customs, anything else under `hooks.Stop` or `hooks.Notification` stays put.

```bash
bash install.sh --clean-sound-hooks      # strip all sound hooks
bash install.sh --with-sound-hooks       # re-add Stop only
```

## Optional: Thinking Summaries (`--with-thinking-summaries`)

Opt-in (off by default). Sets `showThinkingSummaries: true` in `~/.claude/settings.json`. Useful for debugging agent quality — without it, thinking blocks render as collapsed stubs in the interactive UI. Some users find the verbose thinking output noisy; that's why it's opt-in rather than default.

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

One-shot upgrade — preferred entry point:

```bash
cd agentpipe
bash update.sh        # bash on macOS/Linux/WSL/Git Bash
.\update.ps1          # PowerShell on Windows
```

`update.sh` / `update.ps1` are thin wrappers around `install.sh --update` / `install.ps1 -Update` (both still work for backward compat). They forward extra args, so `bash update.sh --target codex --no-claude-md` is valid.

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
