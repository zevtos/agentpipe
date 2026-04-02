#!/usr/bin/env bash
set -euo pipefail

# Claude Code Agent Team — Install Script
# Works on: macOS, Linux, WSL, Git Bash (Windows)
#
# Usage:
#   ./install.sh          # install agents + commands
#   ./install.sh --dry    # show what would be copied
#   ./install.sh --diff   # show differences between repo and installed
#   ./install.sh --pull   # copy installed versions BACK to repo (update repo from live)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTS_SRC="$SCRIPT_DIR/agents"
COMMANDS_SRC="$SCRIPT_DIR/commands"

# Detect target directory
detect_claude_home() {
    # WSL accessing Windows Claude config
    if grep -qi microsoft /proc/version 2>/dev/null; then
        local win_user
        win_user=$(cmd.exe /C "echo %USERNAME%" 2>/dev/null | tr -d '\r' || true)
        if [[ -n "$win_user" && -d "/mnt/c/Users/$win_user/.claude" ]]; then
            echo "/mnt/c/Users/$win_user/.claude"
            return
        fi
    fi

    # Native: macOS / Linux / Git Bash on Windows
    if [[ -d "$HOME/.claude" ]]; then
        echo "$HOME/.claude"
        return
    fi

    # Windows Git Bash with USERPROFILE
    if [[ -n "${USERPROFILE:-}" ]]; then
        local converted
        converted=$(cygpath "$USERPROFILE" 2>/dev/null || echo "$USERPROFILE")
        if [[ -d "$converted/.claude" ]]; then
            echo "$converted/.claude"
            return
        fi
    fi

    echo "$HOME/.claude"
}

CLAUDE_HOME="$(detect_claude_home)"
AGENTS_DST="$CLAUDE_HOME/agents"
COMMANDS_DST="$CLAUDE_HOME/commands"

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

# --- Actions ---

do_install() {
    info "Installing to: $CLAUDE_HOME"
    mkdir -p "$AGENTS_DST" "$COMMANDS_DST"

    local count=0

    for f in "$AGENTS_SRC"/*.md; do
        [[ -f "$f" ]] || continue
        local name
        name=$(basename "$f")
        cp "$f" "$AGENTS_DST/$name"
        log "agents/$name"
        ((count++))
    done

    for f in "$COMMANDS_SRC"/*.md; do
        [[ -f "$f" ]] || continue
        local name
        name=$(basename "$f")
        cp "$f" "$COMMANDS_DST/$name"
        log "commands/$name"
        ((count++))
    done

    echo ""
    info "Installed $count files to $CLAUDE_HOME"
}

do_dry() {
    info "Dry run — would install to: $CLAUDE_HOME"
    echo ""

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
}

do_diff() {
    info "Comparing repo ↔ installed ($CLAUDE_HOME)"
    local has_diff=0

    for dir_pair in "agents:$AGENTS_SRC:$AGENTS_DST" "commands:$COMMANDS_SRC:$COMMANDS_DST"; do
        IFS=: read -r label src dst <<< "$dir_pair"
        for f in "$src"/*.md; do
            [[ -f "$f" ]] || continue
            local name
            name=$(basename "$f")
            if [[ -f "$dst/$name" ]]; then
                if ! diff -q "$f" "$dst/$name" >/dev/null 2>&1; then
                    echo ""
                    warn "$label/$name differs:"
                    diff --color=auto -u "$dst/$name" "$f" || true
                    has_diff=1
                fi
            else
                warn "$label/$name — not installed"
                has_diff=1
            fi
        done
    done

    if [[ $has_diff -eq 0 ]]; then
        log "Everything in sync"
    fi
}

do_pull() {
    info "Pulling installed versions back to repo"
    local count=0

    for f in "$AGENTS_DST"/*.md; do
        [[ -f "$f" ]] || continue
        local name
        name=$(basename "$f")
        cp "$f" "$AGENTS_SRC/$name"
        log "agents/$name ← installed"
        ((count++))
    done

    for f in "$COMMANDS_DST"/*.md; do
        [[ -f "$f" ]] || continue
        local name
        name=$(basename "$f")
        cp "$f" "$COMMANDS_SRC/$name"
        log "commands/$name ← installed"
        ((count++))
    done

    echo ""
    info "Pulled $count files into repo"
}

# --- Main ---

case "${1:-}" in
    --dry)  do_dry ;;
    --diff) do_diff ;;
    --pull) do_pull ;;
    --help|-h)
        echo "Usage: $0 [--dry|--diff|--pull]"
        echo ""
        echo "  (no args)  Install agents + commands to ~/.claude/"
        echo "  --dry      Show what would be copied"
        echo "  --diff     Show differences between repo and installed"
        echo "  --pull     Copy installed versions back to repo"
        exit 0
        ;;
    "")     do_install ;;
    *)      err "Unknown flag: $1"; exit 1 ;;
esac
