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

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target=*)  TARGET="${1#--target=}"; shift ;;
        --target)    TARGET="${2:-}"; shift 2 ;;
        --dry)       ACTION="dry"; shift ;;
        --diff)      ACTION="diff"; shift ;;
        --pull)      ACTION="pull"; shift ;;
        --uninstall) ACTION="uninstall"; shift ;;
        --version|-v)
            echo "agentpipe v$VERSION"
            exit 0
            ;;
        --help|-h)
            cat <<EOF
agentpipe v$VERSION

Usage: bash install.sh [--target <name>] [--dry|--diff|--pull|--uninstall]
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
  --uninstall   Remove installed files
  --version     Show version
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
            log "skills/$name/"
            count=$((count + 1))
        done
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
    fi

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
                log "skills/$name/ ← installed"
                count=$((count + 1))
            fi
        done
    fi

    echo ""
    info "Pulled $count items into repo"
}

# --- Main ---

case "$ACTION" in
    install)   do_install ;;
    dry)       do_dry ;;
    diff)      do_diff ;;
    pull)      do_pull ;;
    uninstall) do_uninstall ;;
esac
