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
#   bash install.sh --no-attribution-fix  # skip Co-Authored-By suppression layer
#   bash install.sh --no-config-defaults  # skip $schema + secret deny-list
#   bash install.sh --version             # show version
#
# Targets:
#   claude (default) — copies agents, commands, and skills to ~/.claude/
#   codex            — copies skills to ~/.agents/skills/ (Codex's open-agent-skills path).
#                      Agents and commands are NOT installed: Codex agents use a different
#                      TOML format and Codex CLI doesn't support custom slash commands.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION=$(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || echo "unknown")
AGENTS_SRC="$SCRIPT_DIR/agents"
COMMANDS_SRC="$SCRIPT_DIR/commands"
SKILLS_SRC="$SCRIPT_DIR/skills"
HOOK_SRC="$SCRIPT_DIR/scripts/git-hooks/commit-msg"
JSON_MERGE="$SCRIPT_DIR/scripts/json-merge.py"
GIT_TEMPLATE_DIR="$HOME/.git-templates"
GIT_HOOK_DST="$GIT_TEMPLATE_DIR/hooks/commit-msg"

# Resolve $HOME or Windows USERPROFILE for the given dotfolder name.
# Used for both ~/.claude (Claude Code) and ~/.agents (Codex skills).
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

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target=*)  TARGET="${1#--target=}"; shift ;;
        --target)    TARGET="${2:-}"; shift 2 ;;
        --dry)       ACTION="dry"; shift ;;
        --diff)      ACTION="diff"; shift ;;
        --pull)      ACTION="pull"; shift ;;
        --update)    ACTION="update"; shift ;;
        --uninstall) ACTION="uninstall"; shift ;;
        --no-attribution-fix) ATTRIBUTION_FIX=0; shift ;;
        --no-config-defaults) CONFIG_DEFAULTS=0; shift ;;
        --version|-v)
            echo "agentpipe v$VERSION"
            exit 0
            ;;
        --help|-h)
            cat <<EOF
agentpipe v$VERSION

Usage: bash install.sh [--target <name>] [--dry|--diff|--pull|--update|--uninstall]
       bash install.sh --version

Targets:
  claude (default)  Install agents + commands + skills to ~/.claude/
  codex             Install skills only to ~/.agents/skills/ (Codex CLI).
                    Codex agents use TOML (different format) and Codex CLI
                    has no custom slash commands; both are skipped.

Actions:
  (no action)   Install
  --dry         Show what would be copied
  --diff        Show differences between repo and installed
  --pull        Copy installed versions back to repo
  --update      git pull --ff-only, then install (one-shot upgrade to latest)
  --uninstall   Remove installed files
  --version     Show version

Options:
  --no-attribution-fix  Skip Co-Authored-By suppression (settings keys + git hook).
                        On by default for --target claude. Always off for codex.
  --no-config-defaults  Skip safe-defaults layer ($schema URL for IDE autocomplete
                        + permissions.deny for .env / *.pem / *.key / secrets/**).
                        On by default for --target claude. Always off for codex.
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
        ;;
    codex)
        # Codex skills live in ~/.agents/skills/ (open-agent-skills standard),
        # NOT in ~/.codex/skills/. ~/.codex/ is for config/agents only.
        BASE="$(detect_home_for .agents)"
        AGENTS_DST=""    # Codex agents are TOML files in ~/.codex/agents/ — out of scope
        COMMANDS_DST=""  # Codex CLI does not support custom slash commands
        SKILLS_DST="$BASE/skills"
        ;;
    *)
        err "Unknown target: $TARGET (use 'claude' or 'codex')"
        exit 1
        ;;
esac

codex_skip_notice() {
    if [[ "$TARGET" == "codex" ]]; then
        warn "Codex CLI has no custom slash commands — skipped commands/"
        warn "Codex agents use a different TOML format — skipped agents/. See README for details."
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
CONFIG_DENY_LIST='["Read(./.env)","Read(./.env.*)","Read(./**/secrets/**)","Read(./**/*.pem)","Read(./**/*.key)"]'

do_config_defaults() {
    config_defaults_active || return 0

    local settings="$BASE/settings.json"
    if ! command -v python3 >/dev/null 2>&1; then
        warn "python3 not found — skipping config-defaults"
        return 0
    fi

    local payload
    payload=$(printf '{"$schema": "%s", "permissions": {"deny": %s}}' "$CONFIG_SCHEMA_URL" "$CONFIG_DENY_LIST")
    if python3 "$JSON_MERGE" --list-union permissions.deny "$settings" "$payload" 2>/dev/null; then
        log "settings/\$schema + permissions.deny (secrets) merged"
    else
        warn "settings.json config-defaults merge failed — leaving file untouched"
    fi
}

do_config_defaults_unfix() {
    config_defaults_active || return 0
    info "note: \$schema + permissions.deny left as-is — edit settings.json to revert"
}

do_config_defaults_dry() {
    config_defaults_active || return 0
    echo "Config-defaults:"
    local settings="$BASE/settings.json"
    if [[ -f "$settings" ]] && grep -Fq "$CONFIG_SCHEMA_URL" "$settings"; then
        echo "  = settings/\$schema (already set)"
    else
        info "  + settings/\$schema=$CONFIG_SCHEMA_URL"
    fi
    if [[ -f "$settings" ]] && grep -Fq 'Read(./**/*.key)' "$settings"; then
        echo "  = settings/permissions.deny secrets (already set)"
    else
        info "  + settings/permissions.deny += [.env, *.pem, *.key, secrets/**]"
    fi
    echo ""
}

# --- Actions ---

do_install() {
    info "Installing agentpipe v$VERSION (target: $TARGET) to: $BASE"
    local count=0

    if [[ -n "$AGENTS_DST" ]]; then
        mkdir -p "$AGENTS_DST"
        for f in "$AGENTS_SRC"/*.md; do
            [[ -f "$f" ]] || continue
            local name
            name=$(basename "$f")
            cp "$f" "$AGENTS_DST/$name"
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

    if [[ -n "$SKILLS_DST" && -d "$SKILLS_SRC" ]]; then
        mkdir -p "$SKILLS_DST"
        for d in "$SKILLS_SRC"/*/; do
            [[ -d "$d" ]] || continue
            local name
            name=$(basename "$d")
            rm -rf "$SKILLS_DST/$name"
            cp -R "$d" "$SKILLS_DST/$name"
            # Скиллы могут держать собственный venv в .venv/ (создаётся
            # bootstrap-скриптом). Не тащим dev-овский venv в системную
            # установку — пути и питон у пользователя другие.
            rm -rf "$SKILLS_DST/$name/.venv" "$SKILLS_DST/$name/.venv.lock"
            log "skills/$name/"
            count=$((count + 1))
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

    echo ""
    info "Installed $count items to $BASE"
    codex_skip_notice
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

    if [[ -n "$SKILLS_DST" && -d "$SKILLS_SRC" ]]; then
        for d in "$SKILLS_SRC"/*/; do
            [[ -d "$d" ]] || continue
            local name
            name=$(basename "$d")
            if [[ -d "$SKILLS_DST/$name" ]]; then
                rm -rf "$SKILLS_DST/$name"
                log "removed skills/$name/"
                count=$((count + 1))
            fi
        done
    fi

    # Remove directories only if empty
    for d in "$AGENTS_DST" "$COMMANDS_DST" "$SKILLS_DST"; do
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
        echo "Agents:"
        for f in "$AGENTS_SRC"/*.md; do
            [[ -f "$f" ]] || continue
            local name
            name=$(basename "$f")
            if [[ -f "$AGENTS_DST/$name" ]]; then
                if diff -q "$f" "$AGENTS_DST/$name" >/dev/null 2>&1; then
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

    if [[ -n "$SKILLS_DST" && -d "$SKILLS_SRC" ]]; then
        echo "Skills:"
        for d in "$SKILLS_SRC"/*/; do
            [[ -d "$d" ]] || continue
            local name
            name=$(basename "$d")
            if [[ -d "$SKILLS_DST/$name" ]]; then
                if diff -rq "$d" "$SKILLS_DST/$name" >/dev/null 2>&1; then
                    echo "  = $name/ (identical)"
                else
                    warn "  ~ $name/ (CHANGED)"
                fi
            else
                info "  + $name/ (NEW)"
            fi
        done
        echo ""
    fi

    do_attribution_dry
    do_config_defaults_dry
    codex_skip_notice
}

do_diff() {
    info "Comparing repo ↔ installed at $BASE (target: $TARGET)"
    local has_diff=0

    if [[ -n "$AGENTS_DST" ]]; then
        for f in "$AGENTS_SRC"/*.md; do
            [[ -f "$f" ]] || continue
            local name
            name=$(basename "$f")
            if [[ -f "$AGENTS_DST/$name" ]]; then
                if ! diff -q "$f" "$AGENTS_DST/$name" >/dev/null 2>&1; then
                    echo ""
                    warn "agents/$name differs:"
                    diff --color=auto -u "$AGENTS_DST/$name" "$f" || true
                    has_diff=1
                fi
            else
                warn "agents/$name — not installed"
                has_diff=1
            fi
        done
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

    if [[ -n "$SKILLS_DST" && -d "$SKILLS_SRC" ]]; then
        for d in "$SKILLS_SRC"/*/; do
            [[ -d "$d" ]] || continue
            local name
            name=$(basename "$d")
            if [[ -d "$SKILLS_DST/$name" ]]; then
                if ! diff -rq "$d" "$SKILLS_DST/$name" >/dev/null 2>&1; then
                    echo ""
                    warn "skills/$name/ differs:"
                    diff --color=auto -ru "$SKILLS_DST/$name" "$d" || true
                    has_diff=1
                fi
            else
                warn "skills/$name/ — not installed"
                has_diff=1
            fi
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
        for f in "$AGENTS_DST"/*.md; do
            [[ -f "$f" ]] || continue
            local name
            name=$(basename "$f")
            cp "$f" "$AGENTS_SRC/$name"
            log "agents/$name ← installed"
            count=$((count + 1))
        done
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

    if [[ -n "$SKILLS_DST" && -d "$SKILLS_SRC" ]]; then
        for d in "$SKILLS_SRC"/*/; do
            [[ -d "$d" ]] || continue
            local name
            name=$(basename "$d")
            if [[ -d "$SKILLS_DST/$name" ]]; then
                rm -rf "$SKILLS_SRC/$name"
                cp -R "$SKILLS_DST/$name" "$SKILLS_SRC/$name"
                # На обратном пути тоже не тянем venv в репо.
                rm -rf "$SKILLS_SRC/$name/.venv" "$SKILLS_SRC/$name/.venv.lock"
                log "skills/$name/ ← installed"
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
    install)   do_install ;;
    dry)       do_dry ;;
    diff)      do_diff ;;
    pull)      do_pull ;;
    update)    do_update ;;
    uninstall) do_uninstall ;;
esac
