#!/usr/bin/env bash
set -euo pipefail

# agentpipe — Install Script
# Works on: macOS, Linux, WSL, Git Bash (Windows)
#
# Usage:
#   bash install.sh                       # install for Claude Code (default)
#   bash install.sh --target codex        # install for Codex CLI (skills only)
#   bash install.sh --dry                 # preview what would change
#   bash install.sh --diff                # show repo vs installed differences
#   bash install.sh --pull                # copy installed back to repo
#   bash install.sh --uninstall           # remove installed files
#   bash install.sh --clean-sound-hooks   # strip Stop+Notification sound hooks from settings.json
#   bash install.sh --no-attribution-fix  # skip Co-Authored-By suppression layer
#   bash install.sh --no-config-defaults  # skip $schema + secret deny-list
#   bash install.sh --no-gost-validation  # skip gost-report Stop-hook validator
#   bash install.sh --skills-only         # copy only skills/* (skip agents, commands, hooks)
#   bash install.sh --with-sound-hooks         # opt-in: Stop sound hook only
#   bash install.sh --with-notification-sound  # opt-in: Notification sound hook only
#   bash install.sh --model-profile opus  # all agents on opus (default: mixed)
#   bash install.sh --version             # show version
#
# Targets:
#   claude (default) — copies agents, commands, and skills to ~/.claude/
#   codex            — copies skills to ~/.agents/skills/ and ~/.codex/skills/.
#                      Agents and commands are NOT installed: Codex agents use a different
#                      TOML format and Codex CLI doesn't support custom slash commands.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION=$(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || echo "unknown")
AGENTS_SRC="$SCRIPT_DIR/agents"
COMMANDS_SRC="$SCRIPT_DIR/commands"
SKILLS_SRC="$SCRIPT_DIR/skills"
HOOK_SRC="$SCRIPT_DIR/scripts/git-hooks/commit-msg"
JSON_MERGE="$SCRIPT_DIR/scripts/json-merge.py"
CLAUDE_MD_SRC="$SCRIPT_DIR/scripts/CLAUDE.md.example"
GIT_TEMPLATE_DIR="$HOME/.git-templates"
GIT_HOOK_DST="$GIT_TEMPLATE_DIR/hooks/commit-msg"

# Resolve $HOME or Windows USERPROFILE for the given dotfolder name.
# Used for ~/.claude (Claude Code). Codex targets intentionally use the
# current shell's $HOME for both ~/.agents and ~/.codex.
detect_home_for() {
    local subdir="$1"  # ".claude" or ".agents"

    # WSL accessing Windows-side dotfolder
    if grep -qi microsoft /proc/version 2>/dev/null; then
        local win_user
        win_user=$(cmd.exe /C "echo %USERNAME%" 2>/dev/null | tr -d '\r' || true)
        if [[ -n "$win_user" && -d "/mnt/c/Users/$win_user/$subdir" ]]; then
            echo "/mnt/c/Users/$win_user/$subdir"
            return
        fi
    fi

    # Native: macOS / Linux / Git Bash on Windows — existing folder wins
    if [[ -d "$HOME/$subdir" ]]; then
        echo "$HOME/$subdir"
        return
    fi

    # Windows Git Bash with USERPROFILE
    if [[ -n "${USERPROFILE:-}" ]]; then
        local converted
        converted=$(cygpath "$USERPROFILE" 2>/dev/null || echo "$USERPROFILE")
        if [[ -d "$converted/$subdir" ]]; then
            echo "$converted/$subdir"
            return
        fi
    fi

    # Fallback: $HOME/$subdir (will be created on install)
    echo "$HOME/$subdir"
}

# Colors (if terminal supports)
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    RED='\033[0;31m'
    CYAN='\033[0;36m'
    NC='\033[0m'
else
    GREEN='' YELLOW='' RED='' CYAN='' NC=''
fi

log()  { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*" >&2; }
info() { echo -e "${CYAN}→${NC} $*"; }

# --- Argument parsing ---

TARGET="claude"
ACTION="install"
ATTRIBUTION_FIX=1
CONFIG_DEFAULTS=1
CLAUDE_MD=1
SOUND_HOOKS=0
NOTIFICATION_SOUND=0
THINKING_SUMMARIES=0
GOST_VALIDATION=1
SKILLS_ONLY=0
MODEL_PROFILE_FLAG=""  # empty = no CLI flag; resolved later from settings.json or default

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target=*)  TARGET="${1#--target=}"; shift ;;
        --target)    TARGET="${2:-}"; shift 2 ;;
        --dry)       ACTION="dry"; shift ;;
        --diff)      ACTION="diff"; shift ;;
        --pull)      ACTION="pull"; shift ;;
        --update)    ACTION="update"; shift ;;
        --uninstall) ACTION="uninstall"; shift ;;
        --clean-sound-hooks) ACTION="clean-sound-hooks"; shift ;;
        --no-attribution-fix) ATTRIBUTION_FIX=0; shift ;;
        --no-config-defaults) CONFIG_DEFAULTS=0; shift ;;
        --no-claude-md) CLAUDE_MD=0; shift ;;
        --no-gost-validation) GOST_VALIDATION=0; shift ;;
        --skills-only) SKILLS_ONLY=1; shift ;;
        --with-sound-hooks) SOUND_HOOKS=1; shift ;;
        --with-notification-sound) NOTIFICATION_SOUND=1; shift ;;
        --with-thinking-summaries) THINKING_SUMMARIES=1; shift ;;
        --model-profile=*) MODEL_PROFILE_FLAG="${1#--model-profile=}"; shift ;;
        --model-profile)   MODEL_PROFILE_FLAG="${2:-}"; shift 2 ;;
        --version|-v)
            echo "agentpipe v$VERSION"
            exit 0
            ;;
        --help|-h)
            cat <<EOF
agentpipe v$VERSION

Usage: bash install.sh [--target <name>] [--dry|--diff|--pull|--update|--uninstall|--clean-sound-hooks]
       bash install.sh --version

Targets:
  claude (default)  Install agents + commands + skills to ~/.claude/
  codex             Install skills only to ~/.agents/skills/ and ~/.codex/skills/ (Codex CLI).
                    Codex agents use TOML (different format) and Codex CLI
                    has no custom slash commands; both are skipped.

Actions:
  (no action)   Install
  --dry         Show what would be copied
  --diff        Show differences between repo and installed
  --pull        Copy installed versions back to repo
  --update      git pull --ff-only, then install (one-shot upgrade to latest)
  --uninstall   Remove installed files
  --clean-sound-hooks  Strip every sound hook entry (Stop+Notification) from
                ~/.claude/settings.json. Leaves non-sound hooks intact
                (gost-validation, user customs). Use to reset before re-adding
                with --with-sound-hooks / --with-notification-sound.
  --version     Show version

Options:
  --no-attribution-fix      Skip Co-Authored-By suppression (settings keys + git hook).
                            On by default for --target claude. Always off for codex.
  --no-config-defaults      Skip safe-defaults layer (\$schema URL, autoUpdatesChannel=stable,
                            cleanupPeriodDays=180, spinnerTipsEnabled=false, permissions.deny
                            for secrets and destructive Bash patterns).
                            On by default for --target claude. Always off for codex.
  --no-claude-md            Don't install ~/.claude/CLAUDE.md.example baseline.
                            Default: install only if ~/.claude/CLAUDE.md does not exist
                            (never overwrites). Always off for codex.
  --no-gost-validation      Skip the deterministic Stop hook that runs gost-report's
                            validate.py against any .docx with a sibling sentinel file.
                            On by default for --target claude — invisible to the model
                            in normal flow, fires only when the generated .docx fails
                            ГОСТ checks. Off by default for codex (Codex has no hooks).
  --skills-only             Copy only skills/* — skip agents, commands, and every
                            settings.json / hook layer (attribution, config-defaults,
                            CLAUDE.md baseline, sound hooks, thinking summaries,
                            gost-validation). Works with both --target claude and
                            --target codex. Composes with --dry / --diff / --pull /
                            --uninstall, scoping each action to skills only.
  --with-sound-hooks        Add a Stop sound hook (one beep when Claude finishes a turn).
                            OS auto-detected: afplay/paplay/powershell beep. Off by default —
                            personal preference. Always off for codex.
  --with-notification-sound Add a Notification sound hook (beep on permission prompts and
                            "waiting for input"). Independent of --with-sound-hooks. Passing
                            both is allowed but warns: Notification often fires right after
                            Stop, producing two beeps in sequence. Always off for codex.
  --with-thinking-summaries Set showThinkingSummaries=true. Off by default — some users
                            find the thinking output noisy. Always off for codex.
  --model-profile <preset>  Per-agent model assignment. Presets: opus (all agents on opus),
                            sonnet (all on sonnet), mixed (default — opus for architect+
                            security, sonnet for the rest, matches agents/*.md source).
                            Persisted to settings.json under agentpipeModelProfile so
                            update.sh reuses the choice. Codex unaffected (agents skipped).
                            Note: opus profile costs ~5× more per session.
EOF
            exit 0
            ;;
        *)
            err "Unknown flag: $1"
            err "Run: bash install.sh --help"
            exit 1
            ;;
    esac
done

# --- Resolve destinations from target ---

case "$TARGET" in
    claude)
        BASE="$(detect_home_for .claude)"
        AGENTS_DST="$BASE/agents"
        COMMANDS_DST="$BASE/commands"
        SKILLS_DST="$BASE/skills"
        SKILLS_EXTRA_DST=""
        ;;
    codex)
        # Keep the open-agent-skills path, and also install to ~/.codex/skills/.
        # Both paths use the current shell's HOME. WSL Codex sessions load
        # Linux-side home paths; Windows-side installs should use install.ps1.
        BASE="$HOME/.agents"
        CODEX_BASE="$HOME/.codex"
        AGENTS_DST=""    # Codex agents are TOML files in ~/.codex/agents/ — out of scope
        COMMANDS_DST=""  # Codex CLI does not support custom slash commands
        SKILLS_DST="$BASE/skills"
        SKILLS_EXTRA_DST="$CODEX_BASE/skills"
        ;;
    *)
        err "Unknown target: $TARGET (use 'claude' or 'codex')"
        exit 1
        ;;
esac

# --skills-only: drop everything except the skills copy. We null out the agent +
# command destinations (every action already gates on `[[ -n "$X_DST" ]]`) and
# turn off every feature-flag layer. Same effect under --target codex (where
# agents/commands are already null) — the flag just additionally suppresses
# settings.json layers if a user opted those in.
if [[ "$SKILLS_ONLY" -eq 1 ]]; then
    AGENTS_DST=""
    COMMANDS_DST=""
    ATTRIBUTION_FIX=0
    CONFIG_DEFAULTS=0
    CLAUDE_MD=0
    SOUND_HOOKS=0
    NOTIFICATION_SOUND=0
    THINKING_SUMMARIES=0
    GOST_VALIDATION=0
fi

skills_only_notice() {
    if [[ "$SKILLS_ONLY" -eq 1 && "$TARGET" == "claude" ]]; then
        warn "--skills-only — skipped agents/, commands/, and all settings.json layers"
    fi
}

codex_skip_notice() {
    if [[ "$TARGET" == "codex" ]]; then
        warn "Codex CLI has no custom slash commands — skipped commands/"
        warn "Codex agents use a different TOML format — skipped agents/. See README for details."
        info "Codex skills installed to both ~/.agents/skills/ and ~/.codex/skills/ for compatibility."
    fi
}

# --- Attribution-fix layer (claude target only) ---
#
# Two independent guards against Claude Code commit trailers:
#  1. settings.json  → attribution.commit/pr=""  (modern key, takes precedence)
#                    + includeCoAuthoredBy=false (deprecated key, kept for backward
#                      compat with older Claude Code that doesn't read attribution)
#  2. ~/.git-templates/hooks/commit-msg  + init.templateDir  (belt-and-suspenders;
#     hook regex matches Co-Authored-By: Claude<anything><noreply@anthropic.com>
#     to catch model-named variants like "Claude Sonnet 4.6")
# Codex target skips both: it doesn't run Claude Code.

attribution_active() {
    [[ "$TARGET" == "claude" && "$ATTRIBUTION_FIX" -eq 1 ]]
}

do_attribution_fix() {
    attribution_active || return 0

    # 1. settings.json — write both keys: modern (attribution) + legacy
    local settings="$BASE/settings.json"
    local attribution_payload='{"attribution": {"commit": "", "pr": ""}, "includeCoAuthoredBy": false}'
    if command -v python3 >/dev/null 2>&1; then
        if python3 "$JSON_MERGE" "$settings" "$attribution_payload" 2>/dev/null; then
            log "settings/attribution=hidden (commit+pr) and includeCoAuthoredBy=false"
        else
            warn "settings.json merge failed — leaving file untouched"
        fi
    else
        warn "python3 not found — skipping settings.json (hook layer still applies)"
    fi

    # 2. Global commit-msg hook via init.templateDir
    mkdir -p "$GIT_TEMPLATE_DIR/hooks"
    if [[ -f "$GIT_HOOK_DST" ]] && cmp -s "$HOOK_SRC" "$GIT_HOOK_DST"; then
        log "git/commit-msg already current"
    else
        if [[ -f "$GIT_HOOK_DST" ]]; then
            local backup="$GIT_HOOK_DST.agentpipe.bak.$(date +%s)"
            mv "$GIT_HOOK_DST" "$backup"
            warn "existing commit-msg hook backed up to $backup"
        fi
        cp "$HOOK_SRC" "$GIT_HOOK_DST"
        chmod +x "$GIT_HOOK_DST"
        log "git/commit-msg installed → $GIT_HOOK_DST"
    fi

    # 3. init.templateDir — set only if unset or already ours
    local current
    current=$(git config --global --get init.templateDir 2>/dev/null || true)
    # Expand ~ for comparison purposes
    local current_expanded="${current/#\~/$HOME}"
    if [[ -z "$current" ]]; then
        git config --global init.templateDir "$GIT_TEMPLATE_DIR"
        log "git/init.templateDir=$GIT_TEMPLATE_DIR"
    elif [[ "$current_expanded" == "$GIT_TEMPLATE_DIR" ]]; then
        log "git/init.templateDir already set"
    else
        warn "init.templateDir already set to: $current"
        warn "  → not overriding. Copy $GIT_HOOK_DST into $current/hooks/ manually."
    fi

    info "note: existing repos are unaffected — run 'git init' inside any repo"
    info "      to apply the hook, or copy the hook into .git/hooks/ manually."
}

do_attribution_unfix() {
    attribution_active || return 0

    if [[ -f "$GIT_HOOK_DST" ]] && cmp -s "$HOOK_SRC" "$GIT_HOOK_DST"; then
        rm "$GIT_HOOK_DST"
        log "removed git/commit-msg"
    fi

    local current
    current=$(git config --global --get init.templateDir 2>/dev/null || true)
    local current_expanded="${current/#\~/$HOME}"
    if [[ "$current_expanded" == "$GIT_TEMPLATE_DIR" ]]; then
        git config --global --unset init.templateDir
        log "unset git/init.templateDir"
    fi

    info "note: settings.json/attribution + includeCoAuthoredBy left as-is — edit manually to revert"
}

do_attribution_dry() {
    attribution_active || return 0
    echo "Attribution-fix:"
    local settings="$BASE/settings.json"
    # Check the modern key (attribution.commit="") as the source of truth.
    if [[ -f "$settings" ]] && python3 -c "import json,sys; d=json.load(open('$settings')); sys.exit(0 if d.get('attribution',{}).get('commit')=='' else 1)" 2>/dev/null; then
        echo "  = settings/attribution=hidden (already set)"
    else
        info "  + settings/attribution=hidden + includeCoAuthoredBy=false"
    fi
    if [[ -f "$GIT_HOOK_DST" ]] && cmp -s "$HOOK_SRC" "$GIT_HOOK_DST"; then
        echo "  = git/commit-msg (identical)"
    elif [[ -f "$GIT_HOOK_DST" ]]; then
        warn "  ~ git/commit-msg (CHANGED — existing hook will be backed up)"
    else
        info "  + git/commit-msg (NEW)"
    fi
    local current
    current=$(git config --global --get init.templateDir 2>/dev/null || true)
    local current_expanded="${current/#\~/$HOME}"
    if [[ "$current_expanded" == "$GIT_TEMPLATE_DIR" ]]; then
        echo "  = git/init.templateDir=$GIT_TEMPLATE_DIR"
    elif [[ -z "$current" ]]; then
        info "  + git/init.templateDir=$GIT_TEMPLATE_DIR"
    else
        warn "  ! git/init.templateDir already set to $current — will not override"
    fi
    echo ""
}

do_attribution_diff() {
    attribution_active || return 0
    if [[ -f "$GIT_HOOK_DST" ]]; then
        if ! cmp -s "$HOOK_SRC" "$GIT_HOOK_DST"; then
            echo ""
            warn "git-hooks/commit-msg differs:"
            diff --color=auto -u "$GIT_HOOK_DST" "$HOOK_SRC" || true
            return 1
        fi
    else
        warn "git-hooks/commit-msg — not installed"
        return 1
    fi
    return 0
}

# --- Config-defaults layer (claude target only) ---
#
# Two universal defaults for ~/.claude/settings.json:
#  1. $schema URL — IDE autocomplete + validation in VS Code, Cursor, etc.
#  2. permissions.deny — universally-unsafe file reads (.env, *.pem, *.key,
#     secrets/**). User's existing entries are preserved (set-union via
#     json-merge.py --list-union). Allow-list is intentionally NOT set: too
#     stack-specific to ship as a default.
# Codex target skips this: settings.json is Claude Code only.

config_defaults_active() {
    [[ "$TARGET" == "claude" && "$CONFIG_DEFAULTS" -eq 1 ]]
}

CONFIG_SCHEMA_URL='https://json.schemastore.org/claude-code-settings.json'
# permissions.deny: secrets + universally-destructive Bash patterns.
# Set-union with user entries (--list-union) so we don't clobber.
CONFIG_DENY_LIST='[
  "Read(./.env)",
  "Read(./.env.*)",
  "Read(./**/secrets/**)",
  "Read(./**/*.pem)",
  "Read(./**/*.key)",
  "Bash(rm -rf /*)",
  "Bash(rm -rf ~/*)",
  "Bash(rm -rf $HOME/*)",
  "Bash(mkfs *)",
  "Bash(dd * of=/dev/*)"
]'

do_config_defaults() {
    config_defaults_active || return 0

    local settings="$BASE/settings.json"
    if ! command -v python3 >/dev/null 2>&1; then
        warn "python3 not found — skipping config-defaults"
        return 0
    fi

    # Top-level keys: $schema + autoUpdatesChannel (skip beta) +
    # cleanupPeriodDays (180 vs default 30) + spinnerTipsEnabled false.
    # Plus permissions.deny set-union via json-merge --list-union.
    local payload
    payload=$(cat <<JSON
{
  "\$schema": "$CONFIG_SCHEMA_URL",
  "autoUpdatesChannel": "stable",
  "cleanupPeriodDays": 180,
  "spinnerTipsEnabled": false,
  "permissions": {
    "deny": $CONFIG_DENY_LIST
  }
}
JSON
)
    if python3 "$JSON_MERGE" --list-union permissions.deny "$settings" "$payload" 2>/dev/null; then
        log "settings/config-defaults merged (\$schema + autoUpdatesChannel + cleanupPeriodDays + spinnerTipsEnabled + permissions.deny)"
    else
        warn "settings.json config-defaults merge failed — leaving file untouched"
    fi
}

do_config_defaults_unfix() {
    config_defaults_active || return 0
    info "note: config-defaults keys left as-is — edit settings.json to revert"
}

# --- CLAUDE.md baseline (claude target only, install-if-missing) ---
# Copies a neutral baseline to ~/.claude/CLAUDE.md ONLY if no file exists.
# Never overwrites — user's existing rules are sacred.

claude_md_active() {
    [[ "$TARGET" == "claude" && "$CLAUDE_MD" -eq 1 ]]
}

do_claude_md() {
    claude_md_active || return 0
    local dst="$BASE/CLAUDE.md"
    if [[ -f "$dst" ]]; then
        log "claude-md/CLAUDE.md already exists — not overwriting"
    else
        mkdir -p "$BASE"
        cp "$CLAUDE_MD_SRC" "$dst"
        log "claude-md/CLAUDE.md installed (neutral baseline) → $dst"
    fi
}

do_claude_md_dry() {
    claude_md_active || return 0
    echo "Claude.md baseline:"
    local dst="$BASE/CLAUDE.md"
    if [[ -f "$dst" ]]; then
        echo "  = CLAUDE.md (already exists, will not overwrite)"
    else
        info "  + CLAUDE.md (neutral baseline, install-if-missing)"
    fi
    echo ""
}

# --- Sound hooks (claude target only, opt-in) ---
# Stop and Notification are independent audible cues. OS auto-detected.
# --with-sound-hooks         → Stop only (one beep when Claude finishes a turn)
# --with-notification-sound  → Notification only (beep on permission/wait-for-input)
# Both flags together print a warning — Notification often fires right after
# Stop, producing two beeps in sequence at the end of a chat.

stop_sound_active() {
    [[ "$TARGET" == "claude" && "$SOUND_HOOKS" -eq 1 ]]
}

notification_sound_active() {
    [[ "$TARGET" == "claude" && "$NOTIFICATION_SOUND" -eq 1 ]]
}

# Returns the OS-appropriate sound command (silenced on missing tool).
sound_command_for() {
    local kind="$1"  # "stop" or "notification"
    case "$(uname -s)" in
        Darwin)
            if [[ "$kind" == "stop" ]]; then
                echo 'afplay /System/Library/Sounds/Hero.aiff 2>/dev/null || true'
            else
                echo 'afplay /System/Library/Sounds/Glass.aiff 2>/dev/null || true'
            fi
            ;;
        Linux)
            # WSL detection: use Windows beep if /proc/version mentions Microsoft
            if grep -qi microsoft /proc/version 2>/dev/null; then
                echo "powershell.exe -c '[console]::beep(800,200)' 2>/dev/null || true"
            else
                echo 'paplay /usr/share/sounds/freedesktop/stereo/complete.oga 2>/dev/null || true'
            fi
            ;;
        *)
            echo 'true'
            ;;
    esac
}

# Internal helper: merge a single sound hook (Stop or Notification) into settings.
# $1 = "Stop" or "Notification", $2 = OS-detected command string.
_merge_sound_hook() {
    local event="$1" cmd="$2"
    if ! command -v python3 >/dev/null 2>&1; then
        warn "python3 not found — skipping sound hook ($event)"
        return 0
    fi
    local settings="$BASE/settings.json"
    local payload
    payload=$(EVENT="$event" CMD="$cmd" python3 -c '
import json, os
print(json.dumps({
    "hooks": {
        os.environ["EVENT"]: [{"hooks": [{"type": "command", "command": os.environ["CMD"]}]}],
    }
}))
')
    if python3 "$JSON_MERGE" --list-union "hooks.$event" "$settings" "$payload" 2>/dev/null; then
        log "settings/hooks.$event (sound: $(uname -s)) merged"
    else
        warn "settings.json sound-hook merge failed ($event)"
    fi
}

do_stop_sound_hook() {
    stop_sound_active || return 0
    _merge_sound_hook Stop "$(sound_command_for stop)"
}

do_notification_sound_hook() {
    notification_sound_active || return 0
    _merge_sound_hook Notification "$(sound_command_for notification)"
}

# Warn when both sound flags are set: Stop and Notification often fire in
# sequence at end-of-chat (Notification = "waiting for input"), so the user
# would hear two beeps. Print once before the merge so the warning is visible.
warn_sound_overlap() {
    if stop_sound_active && notification_sound_active; then
        warn "--with-sound-hooks AND --with-notification-sound both set:"
        warn "  Notification often fires right after Stop, so you may hear"
        warn "  two beeps in sequence at the end of each chat. Pass only one"
        warn "  flag if that's not intended, or run --clean-sound-hooks to reset."
    fi
}

do_stop_sound_hook_dry() {
    stop_sound_active || return 0
    echo "Sound hook (Stop):"
    info "  + settings/hooks.Stop ($(uname -s) auto-detected)"
    echo ""
}

do_notification_sound_hook_dry() {
    notification_sound_active || return 0
    echo "Sound hook (Notification):"
    info "  + settings/hooks.Notification ($(uname -s) auto-detected)"
    echo ""
}

# --clean-sound-hooks action — strip every sound hook entry from settings.json.
# Recognises afplay (macOS), paplay (Linux), [console]::beep / powershell beep
# (WSL + Windows). Leaves non-sound hooks (gost-validation, user customs) alone.
do_clean_sound_hooks() {
    if [[ "$TARGET" != "claude" ]]; then
        info "clean-sound-hooks: no-op for codex (Codex CLI has no hooks)"
        return 0
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        err "python3 not found — cannot clean sound hooks"
        exit 1
    fi
    local settings="$BASE/settings.json"
    if [[ ! -f "$settings" ]]; then
        info "no settings.json at $settings — nothing to clean"
        return 0
    fi
    local removed
    if ! removed=$(python3 "$SCRIPT_DIR/scripts/clean-sound-hooks.py" "$settings"); then
        err "clean-sound-hooks failed"
        exit 1
    fi
    if [[ "$removed" -eq 0 ]]; then
        info "no sound hook entries found in $settings"
    else
        log "removed $removed sound hook entr$( [[ "$removed" -eq 1 ]] && echo y || echo ies ) from $settings"
    fi
}

# --- Thinking-summaries (claude target only, opt-in) ---
thinking_summaries_active() {
    [[ "$TARGET" == "claude" && "$THINKING_SUMMARIES" -eq 1 ]]
}

do_thinking_summaries() {
    thinking_summaries_active || return 0
    if ! command -v python3 >/dev/null 2>&1; then
        warn "python3 not found — skipping --with-thinking-summaries"
        return 0
    fi
    local settings="$BASE/settings.json"
    if python3 "$JSON_MERGE" "$settings" '{"showThinkingSummaries": true}' 2>/dev/null; then
        log "settings/showThinkingSummaries=true"
    else
        warn "settings.json showThinkingSummaries merge failed"
    fi
}

do_thinking_summaries_dry() {
    thinking_summaries_active || return 0
    echo "Thinking summaries:"
    info "  + settings/showThinkingSummaries=true"
    echo ""
}

# --- gost-report validation hook (claude target only, default-on) ---
#
# Stop hook fires once per turn. validate.py scans cwd for *.gost-meta.json
# sentinels (written by gost_report.Report.save()) and validates each .docx
# they describe. On any tier-(a) violation, validate.py prints a JSON
# {"decision":"block","reason":"..."} which Claude Code feeds back to the
# model as a continuation reason. The hook itself always exits 0 — even on
# its own crash — so it can never break the Stop pipeline.
#
# Sentinel scoping: only .docx files with a sibling .gost-meta.json get
# validated. Hooks fire in every project's Claude Code session, but in
# projects that don't use gost-report there are no sentinels and the hook
# is a ~5ms no-op.
#
# Codex target skips this — Codex CLI has no hooks. The validate.py script
# still ships in the codex skill .zip and works in CLI mode (--check) for
# manual debugging.

gost_validation_active() {
    [[ "$TARGET" == "claude" && "$GOST_VALIDATION" -eq 1 ]]
}

do_gost_validation() {
    gost_validation_active || return 0
    if ! command -v python3 >/dev/null 2>&1; then
        warn "python3 not found — skipping gost-validation hook"
        return 0
    fi
    local validate_path="$SKILLS_DST/gost-report/scripts/validate.py"
    if [[ ! -f "$validate_path" ]]; then
        # Skill not installed in this run (--target codex would already have
        # exited via gost_validation_active false; but be defensive).
        return 0
    fi
    local settings="$BASE/settings.json"
    # Build the hook JSON via Python heredoc so the path is opaquely
    # quoted regardless of shell-meta chars in $HOME.
    local payload
    payload=$(VALIDATE_PATH="$validate_path" python3 -c '
import json, os
vp = os.environ["VALIDATE_PATH"]
cmd = f"python3 \"{vp}\" --hook 2>/dev/null || true"
print(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]}}))
')
    if python3 "$JSON_MERGE" --list-union hooks.Stop "$settings" "$payload" 2>/dev/null; then
        log "settings/hooks.Stop += gost-report validate (deterministic, invisible to model)"
    else
        warn "settings.json gost-validation merge failed"
    fi
}

do_gost_validation_dry() {
    gost_validation_active || return 0
    echo "Gost-report validation hook:"
    info "  + settings/hooks.Stop += gost-report validate (deterministic, default-on)"
    echo ""
}

# --- Model-profile layer (claude target only) ---
#
# Three presets:
#   mixed (default) — opus for architect+security, sonnet for the rest.
#                     Matches the source-of-truth model: lines in agents/*.md.
#   opus            — every agent set to opus.
#   sonnet          — every agent downgraded to sonnet.
#
# Source files in agents/ are NEVER modified. The installer rewrites the
# `model:` line at copy time. Choice is persisted to ~/.claude/settings.json
# under the key `agentpipeModelProfile` and reused on subsequent installs
# unless --model-profile is passed again.
#
# Codex target skips this entirely (agents are not installed for codex).

# Canonical (mixed-default) model for an agent name.
canonical_model_for() {
    case "$1" in
        architect|security) echo "opus" ;;
        *) echo "sonnet" ;;
    esac
}

# Resolved model for a given profile + agent name.
model_for_profile() {
    local profile="$1" agent="$2"
    case "$profile" in
        opus|sonnet) echo "$profile" ;;
        *) canonical_model_for "$agent" ;;  # mixed (and any unexpected value) → canonical
    esac
}

# Copy agent file to dst, rewriting the `model:` line per profile.
# Idempotent: re-running with the same profile produces byte-identical output.
apply_model_rewrite() {
    local src="$1" dst="$2" profile="$3"
    local agent_name target_model
    agent_name=$(basename "$src" .md)
    target_model=$(model_for_profile "$profile" "$agent_name")
    sed -E "s/^model: (opus|sonnet|haiku).*/model: $target_model/" "$src" > "$dst"
}

# Read persisted profile from settings.json. Echoes empty string if not set.
read_persisted_profile() {
    local settings="$BASE/settings.json"
    [[ -f "$settings" ]] || { echo ""; return; }
    command -v python3 >/dev/null 2>&1 || { echo ""; return; }
    python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    v = d.get("agentpipeModelProfile", "")
    print(v if v in ("opus", "sonnet", "mixed") else "")
except Exception:
    print("")
' "$settings" 2>/dev/null
}

# Persist chosen profile to settings.json. Skipped on codex / when python3 missing.
persist_profile() {
    local profile="$1"
    [[ "$TARGET" == "claude" ]] || return 0
    command -v python3 >/dev/null 2>&1 || return 0
    python3 "$JSON_MERGE" "$BASE/settings.json" "{\"agentpipeModelProfile\": \"$profile\"}" >/dev/null 2>&1 || true
}

# Resolve MODEL_PROFILE: CLI flag > persisted (settings.json) > default 'mixed'.
if [[ -n "$MODEL_PROFILE_FLAG" ]]; then
    MODEL_PROFILE="$MODEL_PROFILE_FLAG"
elif [[ "$TARGET" == "claude" ]]; then
    persisted=$(read_persisted_profile)
    MODEL_PROFILE="${persisted:-mixed}"
else
    MODEL_PROFILE="mixed"
fi

case "$MODEL_PROFILE" in
    opus|sonnet|mixed) ;;
    *)
        err "Invalid --model-profile: $MODEL_PROFILE (use: opus, sonnet, mixed)"
        exit 1
        ;;
esac

do_config_defaults_dry() {
    config_defaults_active || return 0
    echo "Config-defaults:"
    local settings="$BASE/settings.json"
    if [[ -f "$settings" ]] && grep -Fq "$CONFIG_SCHEMA_URL" "$settings"; then
        echo "  = settings/\$schema (already set)"
    else
        info "  + settings/\$schema=$CONFIG_SCHEMA_URL"
    fi
    if [[ -f "$settings" ]] && grep -Fq '"autoUpdatesChannel": "stable"' "$settings"; then
        echo "  = settings/autoUpdatesChannel=stable (already set)"
    else
        info "  + settings/autoUpdatesChannel=stable (vs default 'latest' beta)"
    fi
    if [[ -f "$settings" ]] && grep -Fq '"cleanupPeriodDays": 180' "$settings"; then
        echo "  = settings/cleanupPeriodDays=180 (already set)"
    else
        info "  + settings/cleanupPeriodDays=180 (vs default 30)"
    fi
    if [[ -f "$settings" ]] && grep -Fq '"spinnerTipsEnabled": false' "$settings"; then
        echo "  = settings/spinnerTipsEnabled=false (already set)"
    else
        info "  + settings/spinnerTipsEnabled=false"
    fi
    if [[ -f "$settings" ]] && grep -Fq 'Bash(rm -rf /*)' "$settings"; then
        echo "  = settings/permissions.deny (secrets + destructive Bash) already set"
    else
        info "  + settings/permissions.deny += [.env, *.pem, *.key, secrets/**, rm -rf /*, mkfs, dd of=/dev/*]"
    fi
    echo ""
}

# --- Actions ---

do_install() {
    if [[ -n "$AGENTS_DST" ]]; then
        info "Installing agentpipe v$VERSION (target: $TARGET, model-profile: $MODEL_PROFILE) to: $BASE"
    else
        info "Installing agentpipe v$VERSION (target: $TARGET) to: $BASE"
        if [[ -n "${SKILLS_EXTRA_DST:-}" ]]; then
            info "Additional Codex skills path: $SKILLS_EXTRA_DST"
        fi
    fi
    local count=0

    if [[ -n "$AGENTS_DST" ]]; then
        mkdir -p "$AGENTS_DST"
        for f in "$AGENTS_SRC"/*.md; do
            [[ -f "$f" ]] || continue
            local name
            name=$(basename "$f")
            apply_model_rewrite "$f" "$AGENTS_DST/$name" "$MODEL_PROFILE"
            log "agents/$name"
            count=$((count + 1))
        done
    fi

    if [[ -n "$COMMANDS_DST" ]]; then
        mkdir -p "$COMMANDS_DST"
        for f in "$COMMANDS_SRC"/*.md; do
            [[ -f "$f" ]] || continue
            local name
            name=$(basename "$f")
            cp "$f" "$COMMANDS_DST/$name"
            log "commands/$name"
            count=$((count + 1))
        done
    fi

    if [[ -d "$SKILLS_SRC" ]]; then
        for skills_dst in "$SKILLS_DST" "${SKILLS_EXTRA_DST:-}"; do
            [[ -n "$skills_dst" ]] || continue
            mkdir -p "$skills_dst"
            for d in "$SKILLS_SRC"/*/; do
                [[ -d "$d" ]] || continue
                local name
                name=$(basename "$d")
                rm -rf "$skills_dst/$name"
                cp -R "$d" "$skills_dst/$name"
                # Скиллы могут держать собственный venv в .venv/ (создаётся
                # bootstrap-скриптом). Не тащим dev-овский venv в системную
                # установку — пути и питон у пользователя другие.
                rm -rf "$skills_dst/$name/.venv" "$skills_dst/$name/.venv.lock"
                log "skills/$name/ → $skills_dst/$name"
                count=$((count + 1))
            done
        done
    fi

    if attribution_active; then
        echo ""
        do_attribution_fix
    fi

    if config_defaults_active; then
        echo ""
        do_config_defaults
    fi

    if claude_md_active; then
        echo ""
        do_claude_md
    fi

    warn_sound_overlap

    if stop_sound_active; then
        echo ""
        do_stop_sound_hook
    fi

    if notification_sound_active; then
        echo ""
        do_notification_sound_hook
    fi

    if thinking_summaries_active; then
        echo ""
        do_thinking_summaries
    fi

    if gost_validation_active; then
        echo ""
        do_gost_validation
    fi

    # Persist profile only when user explicitly passed the flag — implicit defaults
    # don't pollute settings.json. Re-runs without the flag read it back via
    # read_persisted_profile. Skipped under --skills-only: no agents are touched,
    # so the profile choice is meaningless for this run.
    if [[ -n "$MODEL_PROFILE_FLAG" && "$TARGET" == "claude" && "$SKILLS_ONLY" -eq 0 ]]; then
        persist_profile "$MODEL_PROFILE"
    fi

    echo ""
    info "Installed $count items to $BASE"
    codex_skip_notice
    skills_only_notice
    log "agentpipe v$VERSION"
}

do_uninstall() {
    info "Uninstalling agentpipe from: $BASE (target: $TARGET)"
    local count=0

    if [[ -n "$AGENTS_DST" ]]; then
        for f in "$AGENTS_SRC"/*.md; do
            [[ -f "$f" ]] || continue
            local name
            name=$(basename "$f")
            if [[ -f "$AGENTS_DST/$name" ]]; then
                rm "$AGENTS_DST/$name"
                log "removed agents/$name"
                count=$((count + 1))
            fi
        done
    fi

    if [[ -n "$COMMANDS_DST" ]]; then
        for f in "$COMMANDS_SRC"/*.md; do
            [[ -f "$f" ]] || continue
            local name
            name=$(basename "$f")
            if [[ -f "$COMMANDS_DST/$name" ]]; then
                rm "$COMMANDS_DST/$name"
                log "removed commands/$name"
                count=$((count + 1))
            fi
        done
    fi

    if [[ -d "$SKILLS_SRC" ]]; then
        for skills_dst in "$SKILLS_DST" "${SKILLS_EXTRA_DST:-}"; do
            [[ -n "$skills_dst" ]] || continue
            for d in "$SKILLS_SRC"/*/; do
                [[ -d "$d" ]] || continue
                local name
                name=$(basename "$d")
                if [[ -d "$skills_dst/$name" ]]; then
                    rm -rf "$skills_dst/$name"
                    log "removed skills/$name/ from $skills_dst"
                    count=$((count + 1))
                fi
            done
        done
    fi

    # Remove directories only if empty
    for d in "$AGENTS_DST" "$COMMANDS_DST" "$SKILLS_DST" "${SKILLS_EXTRA_DST:-}"; do
        [[ -n "$d" && -d "$d" ]] || continue
        local label
        label=$(basename "$d")
        if rmdir "$d" 2>/dev/null; then
            log "removed $label/"
        else
            warn "$label/ not empty, left in place"
        fi
    done

    if attribution_active; then
        echo ""
        do_attribution_unfix
    fi

    if config_defaults_active; then
        echo ""
        do_config_defaults_unfix
    fi

    echo ""
    info "Removed $count items from $BASE"
}

do_dry() {
    info "Dry run (target: $TARGET) — would install to: $BASE"
    echo ""

    if [[ -n "$AGENTS_DST" ]]; then
        echo "Agents (model-profile: $MODEL_PROFILE):"
        local tmp
        tmp=$(mktemp)
        for f in "$AGENTS_SRC"/*.md; do
            [[ -f "$f" ]] || continue
            local name
            name=$(basename "$f")
            apply_model_rewrite "$f" "$tmp" "$MODEL_PROFILE"
            if [[ -f "$AGENTS_DST/$name" ]]; then
                if diff -q "$tmp" "$AGENTS_DST/$name" >/dev/null 2>&1; then
                    echo "  = $name (identical)"
                else
                    warn "  ~ $name (CHANGED)"
                fi
            else
                info "  + $name (NEW)"
            fi
        done
        rm -f "$tmp"
        echo ""
    fi

    if [[ -n "$COMMANDS_DST" ]]; then
        echo "Commands:"
        for f in "$COMMANDS_SRC"/*.md; do
            [[ -f "$f" ]] || continue
            local name
            name=$(basename "$f")
            if [[ -f "$COMMANDS_DST/$name" ]]; then
                if diff -q "$f" "$COMMANDS_DST/$name" >/dev/null 2>&1; then
                    echo "  = $name (identical)"
                else
                    warn "  ~ $name (CHANGED)"
                fi
            else
                info "  + $name (NEW)"
            fi
        done
        echo ""
    fi

    if [[ -d "$SKILLS_SRC" ]]; then
        for skills_dst in "$SKILLS_DST" "${SKILLS_EXTRA_DST:-}"; do
            [[ -n "$skills_dst" ]] || continue
            echo "Skills ($skills_dst):"
            for d in "$SKILLS_SRC"/*/; do
                [[ -d "$d" ]] || continue
                local name
                name=$(basename "$d")
                if [[ -d "$skills_dst/$name" ]]; then
                    if diff -rq "$d" "$skills_dst/$name" >/dev/null 2>&1; then
                        echo "  = $name/ (identical)"
                    else
                        warn "  ~ $name/ (CHANGED)"
                    fi
                else
                    info "  + $name/ (NEW)"
                fi
            done
            echo ""
        done
    fi

    do_attribution_dry
    do_config_defaults_dry
    do_claude_md_dry
    warn_sound_overlap
    do_stop_sound_hook_dry
    do_notification_sound_hook_dry
    do_thinking_summaries_dry
    do_gost_validation_dry
    codex_skip_notice
    skills_only_notice
}

do_diff() {
    info "Comparing repo ↔ installed at $BASE (target: $TARGET)"
    local has_diff=0

    if [[ -n "$AGENTS_DST" ]]; then
        local tmp
        tmp=$(mktemp)
        for f in "$AGENTS_SRC"/*.md; do
            [[ -f "$f" ]] || continue
            local name
            name=$(basename "$f")
            apply_model_rewrite "$f" "$tmp" "$MODEL_PROFILE"
            if [[ -f "$AGENTS_DST/$name" ]]; then
                if ! diff -q "$tmp" "$AGENTS_DST/$name" >/dev/null 2>&1; then
                    echo ""
                    warn "agents/$name differs (profile: $MODEL_PROFILE):"
                    diff --color=auto -u "$AGENTS_DST/$name" "$tmp" || true
                    has_diff=1
                fi
            else
                warn "agents/$name — not installed"
                has_diff=1
            fi
        done
        rm -f "$tmp"
    fi

    if [[ -n "$COMMANDS_DST" ]]; then
        for f in "$COMMANDS_SRC"/*.md; do
            [[ -f "$f" ]] || continue
            local name
            name=$(basename "$f")
            if [[ -f "$COMMANDS_DST/$name" ]]; then
                if ! diff -q "$f" "$COMMANDS_DST/$name" >/dev/null 2>&1; then
                    echo ""
                    warn "commands/$name differs:"
                    diff --color=auto -u "$COMMANDS_DST/$name" "$f" || true
                    has_diff=1
                fi
            else
                warn "commands/$name — not installed"
                has_diff=1
            fi
        done
    fi

    if [[ -d "$SKILLS_SRC" ]]; then
        for skills_dst in "$SKILLS_DST" "${SKILLS_EXTRA_DST:-}"; do
            [[ -n "$skills_dst" ]] || continue
            for d in "$SKILLS_SRC"/*/; do
                [[ -d "$d" ]] || continue
                local name
                name=$(basename "$d")
                if [[ -d "$skills_dst/$name" ]]; then
                    if ! diff -rq "$d" "$skills_dst/$name" >/dev/null 2>&1; then
                        echo ""
                        warn "skills/$name/ differs at $skills_dst:"
                        diff --color=auto -ru "$skills_dst/$name" "$d" || true
                        has_diff=1
                    fi
                else
                    warn "skills/$name/ — not installed at $skills_dst"
                    has_diff=1
                fi
            done
        done
    fi

    if attribution_active; then
        if ! do_attribution_diff; then
            has_diff=1
        fi
    fi

    if [[ $has_diff -eq 0 ]]; then
        log "Everything in sync"
    fi
}

do_pull() {
    info "Pulling installed versions back to repo (target: $TARGET)"
    local count=0

    if [[ -n "$AGENTS_DST" && -d "$AGENTS_DST" ]]; then
        # Strip user-side profile rewrite back to canonical mixed defaults so the
        # repo source-of-truth never gets contaminated (e.g. all-opus user pulls
        # → canonical opus-for-architect/security, sonnet-for-the-rest).
        local stripped=0
        for f in "$AGENTS_DST"/*.md; do
            [[ -f "$f" ]] || continue
            local name
            name=$(basename "$f")
            apply_model_rewrite "$f" "$AGENTS_SRC/$name" "mixed"
            log "agents/$name ← installed"
            count=$((count + 1))
            stripped=1
        done
        if [[ "$stripped" -eq 1 && "$MODEL_PROFILE" != "mixed" ]]; then
            info "pulled back to canonical mixed defaults — installed profile was $MODEL_PROFILE"
        fi
    fi

    if [[ -n "$COMMANDS_DST" && -d "$COMMANDS_DST" ]]; then
        for f in "$COMMANDS_DST"/*.md; do
            [[ -f "$f" ]] || continue
            local name
            name=$(basename "$f")
            cp "$f" "$COMMANDS_SRC/$name"
            log "commands/$name ← installed"
            count=$((count + 1))
        done
    fi

    local pull_skills_dst="$SKILLS_DST"
    if [[ ! -d "$pull_skills_dst" && -n "${SKILLS_EXTRA_DST:-}" ]]; then
        pull_skills_dst="$SKILLS_EXTRA_DST"
    fi

    if [[ -n "$pull_skills_dst" && -d "$pull_skills_dst" && -d "$SKILLS_SRC" ]]; then
        for d in "$SKILLS_SRC"/*/; do
            [[ -d "$d" ]] || continue
            local name
            name=$(basename "$d")
            if [[ -d "$pull_skills_dst/$name" ]]; then
                rm -rf "$SKILLS_SRC/$name"
                cp -R "$pull_skills_dst/$name" "$SKILLS_SRC/$name"
                # На обратном пути тоже не тянем venv в репо.
                rm -rf "$SKILLS_SRC/$name/.venv" "$SKILLS_SRC/$name/.venv.lock"
                log "skills/$name/ ← installed from $pull_skills_dst"
                count=$((count + 1))
            fi
        done
    fi

    echo ""
    info "Pulled $count items into repo"
}

do_update() {
    info "Updating agentpipe from remote, then installing..."

    if ! git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        err "$SCRIPT_DIR is not a git repository — can't pull."
        err "Re-clone the repo or download a fresh release zip."
        exit 1
    fi

    # Untracked or modified files would block --ff-only or get clobbered.
    if [[ -n "$(git -C "$SCRIPT_DIR" status --porcelain)" ]]; then
        err "Working tree has uncommitted changes. Stash or commit them, then re-run."
        git -C "$SCRIPT_DIR" status --short
        exit 1
    fi

    info "git pull --ff-only"
    if ! git -C "$SCRIPT_DIR" pull --ff-only; then
        err "git pull --ff-only failed (probably divergent history)."
        err "Resolve manually (rebase / merge / reset --hard origin/main) and re-run."
        exit 1
    fi

    # VERSION may have changed in the pulled commits.
    VERSION=$(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || echo "unknown")

    echo ""
    do_install
}

# --- Main ---

case "$ACTION" in
    install)            do_install ;;
    dry)                do_dry ;;
    diff)               do_diff ;;
    pull)               do_pull ;;
    update)             do_update ;;
    uninstall)          do_uninstall ;;
    clean-sound-hooks)  do_clean_sound_hooks ;;
esac
