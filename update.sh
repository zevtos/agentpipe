#!/usr/bin/env bash
# agentpipe — Update Script (canonical update entry point).
# Equivalent to `bash install.sh --update`. Forwards all extra args.
#
# Usage:
#   bash update.sh                          # git pull --ff-only, then install for Claude Code
#   bash update.sh --target codex           # update for Codex CLI
#   bash update.sh --no-claude-md           # skip baseline CLAUDE.md install on update
#   bash update.sh --with-sound-hooks       # opt-in: Stop sound hook only during update
#   bash update.sh --with-notification-sound  # opt-in: Claude Notification sound hook only during update

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/install.sh" --update "$@"
