#!/usr/bin/env bash
set -euo pipefail

# Build release archives for every skill in skills/.
# Produces dist/<skill>.zip with <skill>/ at the top level
# (matches the layout users extract straight into ~/.claude/skills/).
#
# Usage:
#   scripts/build-skills.sh           # build every skill
#   scripts/build-skills.sh <name>    # build only one skill

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILLS_SRC="$SCRIPT_DIR/skills"
DIST="$SCRIPT_DIR/dist"
VERSION=$(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || echo "unknown")

if [[ -t 1 ]]; then
    GREEN='\033[0;32m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
else
    GREEN=''; CYAN=''; RED=''; NC=''
fi

if [[ ! -d "$SKILLS_SRC" ]]; then
    echo -e "${RED}✗${NC} No skills/ directory found at $SKILLS_SRC" >&2
    exit 1
fi

if ! command -v zip >/dev/null 2>&1; then
    echo -e "${RED}✗${NC} 'zip' command not found — install it (apt install zip / brew install zip)" >&2
    exit 1
fi

mkdir -p "$DIST"

target="${1:-}"
count=0

for d in "$SKILLS_SRC"/*/; do
    [[ -d "$d" ]] || continue
    name=$(basename "$d")
    if [[ -n "$target" && "$name" != "$target" ]]; then
        continue
    fi

    if [[ ! -f "$d/SKILL.md" ]]; then
        echo -e "${RED}✗${NC} skills/$name/ has no SKILL.md — skipping" >&2
        continue
    fi

    out="$DIST/$name.zip"
    rm -f "$out"
    (cd "$SKILLS_SRC" && zip -qr "$out" "$name" \
        -x "*.DS_Store" "*/__pycache__/*" "*.pyc" \
           "*/.venv/*" "*/.venv.lock" "*/.installed_hash")
    echo -e "${GREEN}✓${NC} dist/$name.zip"
    count=$((count + 1))
done

if [[ -n "$target" && $count -eq 0 ]]; then
    echo -e "${RED}✗${NC} skill '$target' not found in skills/" >&2
    exit 1
fi

echo ""
echo -e "${CYAN}→${NC} Built $count skill archive(s) in $DIST/ (agentpipe v$VERSION)"
