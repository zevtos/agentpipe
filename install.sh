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
#   bash install.sh --clean-sound-hooks   # strip Stop+Notification sound hooks from settings/hooks config
#   bash install.sh --no-attribution-fix  # skip Co-Authored-By suppression layer
#   bash install.sh --no-config-defaults  # skip $schema + secret deny-list
#   bash install.sh --no-gost-validation  # skip gost-report Stop-hook validator
#   bash install.sh --skills-only         # copy only skills/* (skip agents, commands, hooks)
#   bash install.sh --with-sound-hooks         # opt-in: Stop sound hook only
#   bash install.sh --with-notification-sound  # opt-in: Claude Notification sound hook only
#   bash install.sh --preset god          # bundle: everything + extras + opus + MinerU (gated)
#   bash install.sh --preset senior       # bundle: default + Stop sound + thinking + maxed env
#   bash install.sh --preset minimum      # bundle: tools + safety only, no global git/hook mutation
#   bash install.sh --with-mineru         # pre-warm doc2kb MinerU tier (gated, ~3 GB)
#   bash install.sh --model-profile opus  # all agents on opus (default: mixed)
#   bash install.sh --version             # show version
#
# Presets (an escalating ladder; set per-layer defaults; explicit flags override,
# --skills-only wins). Resolved manifest is printed before install and under --dry:
#   minimum    — tools + safety: agents/commands/skills + launchers + config-defaults
#                (security deny-list) + gost-config. OFF: attribution-fix, claude-md,
#                gost-validation (no global git / settings-hook mutation).
#   default    — the no-flag baseline (every default-on layer), named so it prints.
#   senior     — default + Stop sound + thinking summaries + maxed env defaults
#                (CLAUDE_CODE_EFFORT_LEVEL=xhigh, disable adaptive thinking + non-
#                essential traffic, merged into settings.json "env"). Still sonnet/mixed.
#   god        — senior + ccstatusline + caveman + --model-profile opus + MinerU
#                pre-warm. The three external installs (caveman, MinerU; ccstatusline
#                via runtime npx) are gated — caveman/MinerU need an interactive y/N.
#   codex-full — Codex-native bundle: skills + gost-config + Stop sound + launchers
#                (implies --target codex unless --target is given).
#
# Targets:
#   claude (default) — copies agents, commands, and skills to ~/.claude/
#   codex            — copies skills to ~/.codex/skills/.
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
GOST_CONFIG_SRC="$SCRIPT_DIR/skills/gost-report/scripts/config.env.example"
GIT_TEMPLATE_DIR="$HOME/.git-templates"
GIT_HOOK_DST="$GIT_TEMPLATE_DIR/hooks/commit-msg"

# Resolve $HOME or Windows USERPROFILE for the given dotfolder name.
# Used for ~/.claude (Claude Code). Codex targets intentionally use the
# current shell's $HOME for ~/.codex.
detect_home_for() {
    local subdir="$1"  # ".claude"

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
LAUNCHERS=1
MINERU_PREWARM=0
ENV_DEFAULTS=0         # merge maxed perf/privacy env into settings.json "env"
CCSTATUSLINE=0         # add ccstatusline statusLine block to settings.json (install-if-missing)
CAVEMAN=0             # install caveman (third-party curl|bash, gated like MinerU)
MODEL_PROFILE_FLAG=""  # empty = no CLI flag; resolved later from settings.json or default
PRESET=""              # empty = no preset; resolved after parse, before target resolution

# "was-set" bits: 1 once the user passes the matching flag explicitly. The preset
# resolver only fills layers the user did NOT set, so `--preset god --no-launchers`
# = god minus launchers. Precedence (low→high): target rules < preset < explicit
# flags < --skills-only.
TARGET_SET=0
ATTRIBUTION_FIX_SET=0
CONFIG_DEFAULTS_SET=0
CLAUDE_MD_SET=0
SOUND_HOOKS_SET=0
NOTIFICATION_SOUND_SET=0
THINKING_SUMMARIES_SET=0
GOST_VALIDATION_SET=0
LAUNCHERS_SET=0
MINERU_PREWARM_SET=0
ENV_DEFAULTS_SET=0
CCSTATUSLINE_SET=0
CAVEMAN_SET=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target=*)  TARGET="${1#--target=}"; TARGET_SET=1; shift ;;
        --target)    TARGET="${2:-}"; TARGET_SET=1; shift 2 ;;
        --dry)       ACTION="dry"; shift ;;
        --diff)      ACTION="diff"; shift ;;
        --pull)      ACTION="pull"; shift ;;
        --update)    ACTION="update"; shift ;;
        --uninstall) ACTION="uninstall"; shift ;;
        --clean-sound-hooks) ACTION="clean-sound-hooks"; shift ;;
        --preset=*)  PRESET="${1#--preset=}"; shift ;;
        --preset)    PRESET="${2:-}"; shift 2 ;;
        --no-attribution-fix) ATTRIBUTION_FIX=0; ATTRIBUTION_FIX_SET=1; shift ;;
        --no-config-defaults) CONFIG_DEFAULTS=0; CONFIG_DEFAULTS_SET=1; shift ;;
        --no-claude-md) CLAUDE_MD=0; CLAUDE_MD_SET=1; shift ;;
        --no-gost-validation) GOST_VALIDATION=0; GOST_VALIDATION_SET=1; shift ;;
        --skills-only) SKILLS_ONLY=1; shift ;;
        --no-launchers) LAUNCHERS=0; LAUNCHERS_SET=1; shift ;;
        --with-sound-hooks) SOUND_HOOKS=1; SOUND_HOOKS_SET=1; shift ;;
        --with-notification-sound) NOTIFICATION_SOUND=1; NOTIFICATION_SOUND_SET=1; shift ;;
        --with-thinking-summaries) THINKING_SUMMARIES=1; THINKING_SUMMARIES_SET=1; shift ;;
        --with-mineru) MINERU_PREWARM=1; MINERU_PREWARM_SET=1; shift ;;
        --no-mineru)   MINERU_PREWARM=0; MINERU_PREWARM_SET=1; shift ;;
        --with-env-defaults) ENV_DEFAULTS=1; ENV_DEFAULTS_SET=1; shift ;;
        --no-env-defaults)   ENV_DEFAULTS=0; ENV_DEFAULTS_SET=1; shift ;;
        --with-ccstatusline) CCSTATUSLINE=1; CCSTATUSLINE_SET=1; shift ;;
        --no-ccstatusline)   CCSTATUSLINE=0; CCSTATUSLINE_SET=1; shift ;;
        --with-caveman) CAVEMAN=1; CAVEMAN_SET=1; shift ;;
        --no-caveman)   CAVEMAN=0; CAVEMAN_SET=1; shift ;;
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
  codex             Install skills only to ~/.codex/skills/ (Codex CLI).
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
                ~/.claude/settings.json or ~/.codex/hooks.json. Leaves non-sound hooks intact
                (gost-validation, user customs). Use to reset before re-adding
                with --with-sound-hooks / --with-notification-sound.
  --version     Show version

Presets (--preset <name>): one bundle instead of stacking flags. A preset sets
  per-layer DEFAULTS; any explicit flag overrides the preset for that layer, and
  --skills-only still wins over everything. Precedence: target < preset < flags <
  --skills-only. The resolved manifest is printed before install (and under --dry).
  An escalating ladder: minimum < default < senior < god.
  minimum     Tools + safety: agents/commands/skills + launchers + config-defaults
              (security deny-list) + gost-config. OFF: attribution-fix, claude-md,
              gost-validation — no global git / settings-hook mutation.
  default     The no-flag baseline (every default-on layer), named so it prints.
  senior      default + Stop sound + thinking summaries + maxed env defaults
              (CLAUDE_CODE_EFFORT_LEVEL=xhigh + disable adaptive thinking + disable
              non-essential traffic, merged into settings.json "env"). Stays mixed.
  god         senior + ccstatusline + caveman + --model-profile opus + MinerU
              pre-warm. caveman and MinerU are gated by an interactive y/N confirm.
  codex-full  Codex-native bundle (implies --target codex unless --target is set):
              skills + gost-config + Stop sound + launchers.

Options:
  --preset <name>           Apply a preset bundle (minimum|default|senior|god|codex-full).
  --with-env-defaults       Merge maxed perf/privacy env vars into settings.json "env":
                            CLAUDE_CODE_EFFORT_LEVEL=xhigh, DISABLE_ADAPTIVE_THINKING=1,
                            DISABLE_NONESSENTIAL_TRAFFIC=1. No secrets. senior/god imply it.
                            Note: xhigh raises per-turn reasoning (and quota use).
  --no-env-defaults         Skip the env-defaults merge (overrides a preset).
  --with-ccstatusline       Add a ccstatusline statusLine to settings.json (runs
                            'npx -y ccstatusline@latest'; needs node/npx). Install-if-
                            missing — never clobbers an existing statusLine. god implies it.
  --no-ccstatusline         Skip ccstatusline (overrides a preset).
  --with-caveman            Install caveman (THIRD-PARTY: pipes
                            github.com/JuliusBrussee/caveman main install.sh to bash,
                            needs node>=18). Remote code execution: requires an
                            interactive y/N confirm (default N), skipped non-interactively.
                            god implies it.
  --no-caveman              Never install caveman (overrides a preset).
  --with-mineru             Pre-warm doc2kb's MinerU tier (~3 GB+, several minutes,
                            MLX/CUDA wheels). HEAVY deps are never auto-installed:
                            requires an interactive y/N confirm (default N) and is
                            skipped in non-interactive shells. god implies this.
  --no-mineru               Never pre-warm MinerU (overrides a preset that requests it).
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
                            ГОСТ checks. Not installed for codex.
  --skills-only             Copy only skills/* — skip agents, commands, and every
                            settings.json / hook layer (attribution, config-defaults,
                            CLAUDE.md baseline, sound hooks, thinking summaries,
                            gost-validation). Works with both --target claude and
                            --target codex. Composes with --dry / --diff / --pull /
                            --uninstall, scoping each action to skills only.
  --no-launchers            Skip installing the gr/us/dkb CLI launchers onto PATH
                            (~/.local/bin by default; \$AGENTPIPE_BIN_DIR to override).
                            Launchers install with the skills unless a same-named
                            command already exists on PATH (then skipped, never clobbered).
  --with-sound-hooks        Add a Stop sound hook (one beep when Claude/Codex finishes a turn).
                            OS auto-detected: afplay/paplay/powershell beep. Off by default —
                            personal preference.
  --with-notification-sound Add a Notification sound hook (beep on permission prompts and
                            "waiting for input"). Independent of --with-sound-hooks. Passing
                            both is allowed but warns: Notification often fires right after
                            Stop, producing two beeps in sequence. Claude target only.
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

# --- Preset resolution (before target/destination resolution) ---
#
# A preset fills per-layer DEFAULTS for every layer the user did NOT set
# explicitly (tracked via the *_SET bits). Explicit flags therefore always win,
# and `--skills-only` (applied later) wins over everything. codex-full also flips
# the target to codex unless the user passed --target.

# Set VAR=VALUE only if the matching *_SET bit is 0 (user left it at default).
_preset_set() {  # $1=var  $2=value  $3=setbit-var
    [[ "${!3}" -eq 1 ]] && return 0
    printf -v "$1" '%s' "$2"
}

apply_preset() {
    case "$1" in
        minimum)
            # Tools + safety only. config-defaults (deny-list) and gost-config stay
            # on (gost-config has no toggle anyway). Strip the layers that mutate
            # global git config or install session hooks.
            _preset_set ATTRIBUTION_FIX 0 ATTRIBUTION_FIX_SET
            _preset_set CLAUDE_MD 0 CLAUDE_MD_SET
            _preset_set GOST_VALIDATION 0 GOST_VALIDATION_SET
            ;;
        default)
            : # baseline — every layer already at its literal default
            ;;
        senior)
            _preset_set SOUND_HOOKS 1 SOUND_HOOKS_SET
            _preset_set THINKING_SUMMARIES 1 THINKING_SUMMARIES_SET
            _preset_set ENV_DEFAULTS 1 ENV_DEFAULTS_SET
            ;;
        god)
            # Everything senior gives, made explicit, plus the extras.
            _preset_set ATTRIBUTION_FIX 1 ATTRIBUTION_FIX_SET
            _preset_set CONFIG_DEFAULTS 1 CONFIG_DEFAULTS_SET
            _preset_set CLAUDE_MD 1 CLAUDE_MD_SET
            _preset_set GOST_VALIDATION 1 GOST_VALIDATION_SET
            _preset_set LAUNCHERS 1 LAUNCHERS_SET
            _preset_set SOUND_HOOKS 1 SOUND_HOOKS_SET
            _preset_set THINKING_SUMMARIES 1 THINKING_SUMMARIES_SET
            _preset_set ENV_DEFAULTS 1 ENV_DEFAULTS_SET
            _preset_set CCSTATUSLINE 1 CCSTATUSLINE_SET
            _preset_set CAVEMAN 1 CAVEMAN_SET
            _preset_set MINERU_PREWARM 1 MINERU_PREWARM_SET
            # Notification sound intentionally left off: it duplicates the Stop beep.
            # Model profile → opus unless the user passed --model-profile.
            # (if/fi, not `&&`: a false test as the branch's last command would
            #  return non-zero and trip set -e.)
            if [[ -z "$MODEL_PROFILE_FLAG" ]]; then MODEL_PROFILE_FLAG="opus"; fi
            ;;
        codex-full)
            if [[ "$TARGET_SET" -eq 0 ]]; then TARGET="codex"; fi
            _preset_set SOUND_HOOKS 1 SOUND_HOOKS_SET
            _preset_set LAUNCHERS 1 LAUNCHERS_SET
            ;;
    esac
}

if [[ -n "$PRESET" ]]; then
    case "$PRESET" in
        minimum|default|senior|god|codex-full) apply_preset "$PRESET" ;;
        *)
            err "Unknown --preset: $PRESET (use: minimum, default, senior, god, codex-full)"
            exit 1
            ;;
    esac
fi

# --- Resolve destinations from target ---

case "$TARGET" in
    claude)
        BASE="$(detect_home_for .claude)"
        AGENTS_DST="$BASE/agents"
        COMMANDS_DST="$BASE/commands"
        SKILLS_DST="$BASE/skills"
        LEGACY_CODEX_SKILLS_DST=""
        ;;
    codex)
        # Codex skills live in ~/.codex/skills/. WSL Codex sessions load
        # Linux-side home paths; Windows-side installs should use install.ps1.
        BASE="$HOME/.codex"
        AGENTS_DST=""    # Codex agents are TOML files in ~/.codex/agents/ — out of scope
        COMMANDS_DST=""  # Codex CLI does not support custom slash commands
        SKILLS_DST="$BASE/skills"
        LEGACY_CODEX_SKILLS_DST="$HOME/.agents/skills"
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
# settings/hook layers if a user opted those in.
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
    ENV_DEFAULTS=0
    CCSTATUSLINE=0
    CAVEMAN=0
fi

skills_only_notice() {
    if [[ "$SKILLS_ONLY" -eq 1 && "$TARGET" == "claude" ]]; then
        warn "--skills-only — skipped agents/, commands/, and all settings/hook layers"
    fi
}

codex_skip_notice() {
    if [[ "$TARGET" == "codex" ]]; then
        warn "Codex CLI has no custom slash commands — skipped commands/"
        warn "Codex agents use a different TOML format — skipped agents/. See README for details."
        info "Codex skills installed to ~/.codex/skills/."
    fi
}

legacy_codex_cleanup_active() {
    [[ "$TARGET" == "codex" && -n "${LEGACY_CODEX_SKILLS_DST:-}" && -d "$LEGACY_CODEX_SKILLS_DST" && -d "$SKILLS_SRC" ]]
}

cleanup_empty_legacy_codex_dirs() {
    local skills_dir="${LEGACY_CODEX_SKILLS_DST:-}"
    [[ -n "$skills_dir" ]] || return 0

    if [[ -d "$skills_dir" ]]; then
        rmdir "$skills_dir" 2>/dev/null && log "removed legacy .agents/skills/"
    fi

    local agents_dir
    agents_dir="$(dirname "$skills_dir")"
    if [[ -d "$agents_dir" ]]; then
        rmdir "$agents_dir" 2>/dev/null && log "removed legacy .agents/"
    fi

    return 0
}

cleanup_legacy_codex_skills() {
    LEGACY_CODEX_CLEANED_COUNT=0
    legacy_codex_cleanup_active || return 0

    for d in "$SKILLS_SRC"/*/; do
        [[ -d "$d" ]] || continue
        local name
        name=$(basename "$d")
        if [[ -d "$LEGACY_CODEX_SKILLS_DST/$name" ]]; then
            rm -rf "$LEGACY_CODEX_SKILLS_DST/$name"
            log "removed legacy .agents/skills/$name/"
            LEGACY_CODEX_CLEANED_COUNT=$((LEGACY_CODEX_CLEANED_COUNT + 1))
        fi
    done

    cleanup_empty_legacy_codex_dirs
    return 0
}

dry_legacy_codex_cleanup() {
    legacy_codex_cleanup_active || return 0

    local shown=0
    for d in "$SKILLS_SRC"/*/; do
        [[ -d "$d" ]] || continue
        local name
        name=$(basename "$d")
        if [[ -d "$LEGACY_CODEX_SKILLS_DST/$name" ]]; then
            if [[ "$shown" -eq 0 ]]; then
                echo "Legacy Codex cleanup ($LEGACY_CODEX_SKILLS_DST):"
                shown=1
            fi
            warn "  - $name/ (remove old .agents copy)"
        fi
    done

    if [[ "$shown" -eq 1 ]]; then
        echo ""
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

# --- Env-defaults layer (claude target only, opt-in; senior/god) ---
#
# Merges a maxed perf/privacy block into settings.json "env". settings.json env
# is injected into every session, so no shell-rc mutation. No secrets shipped
# (ultrasearch API keys stay manual). xhigh raises per-turn reasoning AND quota.

ENV_DEFAULTS_PAYLOAD='{"env": {"CLAUDE_CODE_EFFORT_LEVEL": "xhigh", "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}}'

env_defaults_active() {
    [[ "$TARGET" == "claude" && "$ENV_DEFAULTS" -eq 1 ]]
}

do_env_defaults() {
    env_defaults_active || return 0
    if ! command -v python3 >/dev/null 2>&1; then
        warn "python3 not found — skipping env-defaults"
        return 0
    fi
    local settings="$BASE/settings.json"
    if python3 "$JSON_MERGE" "$settings" "$ENV_DEFAULTS_PAYLOAD" 2>/dev/null; then
        log "settings/env merged (CLAUDE_CODE_EFFORT_LEVEL=xhigh + disable adaptive thinking + non-essential traffic)"
    else
        warn "settings.json env-defaults merge failed"
    fi
}

do_env_defaults_dry() {
    env_defaults_active || return 0
    echo "Env defaults:"
    info "  + settings/env += CLAUDE_CODE_EFFORT_LEVEL=xhigh, DISABLE_ADAPTIVE_THINKING=1, DISABLE_NONESSENTIAL_TRAFFIC=1"
    echo ""
}

# --- ccstatusline layer (claude target only, opt-in; god) ---
#
# Adds a statusLine block to settings.json that runs ccstatusline via npx at
# render time (no install-time download; needs node/npx on PATH to render).
# Install-if-missing: never clobbers a statusLine the user already configured.

CCSTATUSLINE_PAYLOAD='{"statusLine": {"type": "command", "command": "npx -y ccstatusline@latest", "padding": 0, "refreshInterval": 10}}'

ccstatusline_active() {
    [[ "$TARGET" == "claude" && "$CCSTATUSLINE" -eq 1 ]]
}

_has_statusline() {
    local settings="$BASE/settings.json"
    [[ -f "$settings" ]] || return 1
    command -v python3 >/dev/null 2>&1 || return 1
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("statusLine") else 1)' "$settings" 2>/dev/null
}

do_ccstatusline() {
    ccstatusline_active || return 0
    if ! command -v python3 >/dev/null 2>&1; then
        warn "python3 not found — skipping ccstatusline"
        return 0
    fi
    if _has_statusline; then
        log "ccstatusline — statusLine already set, leaving as-is"
        return 0
    fi
    local settings="$BASE/settings.json"
    if python3 "$JSON_MERGE" "$settings" "$CCSTATUSLINE_PAYLOAD" 2>/dev/null; then
        log "settings/statusLine = ccstatusline (npx -y ccstatusline@latest)"
        command -v npx >/dev/null 2>&1 || warn "  note: 'npx' not on PATH — install Node.js for the statusline to render"
    else
        warn "settings.json ccstatusline merge failed"
    fi
}

do_ccstatusline_dry() {
    ccstatusline_active || return 0
    echo "ccstatusline:"
    if _has_statusline; then
        echo "  = statusLine already set (will not overwrite)"
    else
        info "  + settings/statusLine = ccstatusline (npx -y ccstatusline@latest)"
    fi
    echo ""
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

# --- gost-report persona config (XDG, install-if-missing) ---
# Skill reads ~/.config/gost-report/config (or $XDG_CONFIG_HOME/...) at runtime
# to fill TitleConfig defaults: FIO, group, teacher. Path is independent of
# the skill install location, so it works for both claude and codex targets.

gost_config_path() {
    if [[ -n "${GOST_REPORT_CONFIG:-}" ]]; then
        echo "$GOST_REPORT_CONFIG"
    elif [[ -n "${XDG_CONFIG_HOME:-}" ]]; then
        echo "$XDG_CONFIG_HOME/gost-report/config"
    else
        echo "$HOME/.config/gost-report/config"
    fi
}

gost_config_active() {
    [[ -n "$SKILLS_DST" && -f "$GOST_CONFIG_SRC" ]]
}

do_gost_config() {
    gost_config_active || return 0
    local dst
    dst=$(gost_config_path)
    if [[ -f "$dst" ]]; then
        log "gost-report/config already exists → $dst (not overwriting)"
    else
        mkdir -p "$(dirname "$dst")"
        cp "$GOST_CONFIG_SRC" "$dst"
        log "gost-report/config installed (template) → $dst"
        info "  edit $dst to set FIO/group/teacher once, then omit them from build.py"
    fi
}

do_gost_config_dry() {
    gost_config_active || return 0
    echo "gost-report persona config:"
    local dst
    dst=$(gost_config_path)
    if [[ -f "$dst" ]]; then
        echo "  = $dst (already exists, will not overwrite)"
    else
        info "  + $dst (template, install-if-missing)"
    fi
    echo ""
}

# --- Sound hooks (opt-in) ---
# Stop and Notification are independent audible cues. OS auto-detected.
# --with-sound-hooks         → Stop only (one beep when Claude/Codex finishes a turn)
# --with-notification-sound  → Notification only (Claude permission/wait-for-input)
# Both flags together print a warning — Notification often fires right after
# Stop, producing two beeps in sequence at the end of a chat.

stop_sound_active() {
    [[ ( "$TARGET" == "claude" || "$TARGET" == "codex" ) && "$SOUND_HOOKS" -eq 1 ]]
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

# Internal helper: merge a single sound hook (Stop or Notification) into hook config.
# $1 = "Stop" or "Notification", $2 = OS-detected command string.
_merge_sound_hook() {
    local event="$1" cmd="$2"
    if ! command -v python3 >/dev/null 2>&1; then
        warn "python3 not found — skipping sound hook ($event)"
        return 0
    fi
    local hook_config="$BASE/settings.json"
    local hook_label="settings/hooks"
    if [[ "$TARGET" == "codex" ]]; then
        hook_config="$BASE/hooks.json"
        hook_label="hooks"
    fi
    local payload
    payload=$(EVENT="$event" CMD="$cmd" python3 -c '
import json, os
print(json.dumps({
    "hooks": {
        os.environ["EVENT"]: [{"hooks": [{"type": "command", "command": os.environ["CMD"]}]}],
    }
}))
')
    if python3 "$JSON_MERGE" --list-union "hooks.$event" "$hook_config" "$payload" 2>/dev/null; then
        log "$hook_label.$event (sound: $(uname -s)) merged"
    else
        warn "$(basename "$hook_config") sound-hook merge failed ($event)"
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
    if [[ "$TARGET" == "codex" ]]; then
        info "  + hooks.json/hooks.Stop ($(uname -s) auto-detected)"
    else
        info "  + settings/hooks.Stop ($(uname -s) auto-detected)"
    fi
    echo ""
}

do_notification_sound_hook_dry() {
    notification_sound_active || return 0
    echo "Sound hook (Notification):"
    info "  + settings/hooks.Notification ($(uname -s) auto-detected)"
    echo ""
}

# --clean-sound-hooks action — strip every sound hook entry from hook config.
# Recognises afplay (macOS), paplay (Linux), [console]::beep / powershell beep
# (WSL + Windows). Leaves non-sound hooks (gost-validation, user customs) alone.
do_clean_sound_hooks() {
    if ! command -v python3 >/dev/null 2>&1; then
        err "python3 not found — cannot clean sound hooks"
        exit 1
    fi
    local hook_config="$BASE/settings.json"
    if [[ "$TARGET" == "codex" ]]; then
        hook_config="$BASE/hooks.json"
    fi
    if [[ ! -f "$hook_config" ]]; then
        info "no $(basename "$hook_config") at $hook_config — nothing to clean"
        return 0
    fi
    local removed
    if ! removed=$(python3 "$SCRIPT_DIR/scripts/clean-sound-hooks.py" "$hook_config"); then
        err "clean-sound-hooks failed"
        exit 1
    fi
    if [[ "$removed" -eq 0 ]]; then
        info "no sound hook entries found in $hook_config"
    else
        log "removed $removed sound hook entr$( [[ "$removed" -eq 1 ]] && echo y || echo ies ) from $hook_config"
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
# validated. Hooks fire in every project's Claude Code session. validate.py
# first checks cwd is under a project root (skips $HOME / "/" outright), then
# walks DOWN with a depth cap (HOOK_GLOB_MAX_DEPTH) skipping node_modules/.git/
# etc — so it never traverses a whole filesystem subtree. In projects without
# gost-report there are no sentinels and the hook is a fast no-op.
#
# Codex target skips this validation layer. The validate.py script
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

# --- CLI launchers (gr/us/dkb on PATH) ---
#
# Short commands so agents (and humans) call `gr build.py` instead of the long
# `python3 ~/.claude/skills/gost-report/scripts/ensure_env.py build.py`. Each shim
# bakes in the installed skill path (env-overridable) and execs the skill entry.
# Installed with the skills unless a same-named command already exists on PATH —
# then skipped, never clobbered (marker-detected so reinstall still updates ours).

LAUNCHER_MARK="# agentpipe-launcher"

launchers_active() { [[ "$LAUNCHERS" -eq 1 && -n "$SKILLS_DST" ]]; }

launcher_bin_dir() { echo "${AGENTPIPE_BIN_DIR:-$HOME/.local/bin}"; }

_gr_shim() {
    cat <<SHIM
#!/usr/bin/env bash
$LAUNCHER_MARK
set -euo pipefail
SKILL="\${GOST_REPORT_SKILL:-$SKILLS_DST/gost-report}"
exec python3 "\$SKILL/scripts/cli.py" "\$@"
SHIM
}

_us_shim() {
    cat <<SHIM
#!/usr/bin/env bash
$LAUNCHER_MARK
set -euo pipefail
SKILL="\${ULTRASEARCH_SKILL:-$SKILLS_DST/ultrasearch}"
exec python3 "\$SKILL/scripts/ensure_env.py" ultrasearch.py "\$@"
SHIM
}

_dkb_shim() {
    cat <<SHIM
#!/usr/bin/env bash
$LAUNCHER_MARK
set -euo pipefail
SKILL="\${DOC2KB_SKILL:-$SKILLS_DST/doc2kb}"
exec python3 "\$SKILL/scripts/dkb.py" "\$@"
SHIM
}

write_launcher() {
    local cmd="$1" content="$2"
    local bindir target found
    bindir="$(launcher_bin_dir)"
    target="$bindir/$cmd"
    if found="$(command -v "$cmd" 2>/dev/null)"; then
        if [[ "$found" != "$target" ]] && ! grep -q "$LAUNCHER_MARK" "$found" 2>/dev/null; then
            warn "launcher '$cmd' skipped — '$found' already on PATH (not agentpipe's)"
            return 0
        fi
    fi
    mkdir -p "$bindir"
    printf '%s\n' "$content" > "$target"
    chmod +x "$target" 2>/dev/null || true
    log "launcher $cmd → $target"
    LAUNCHER_INSTALLED=1
}

launcher_path_notice() {
    local bindir; bindir="$(launcher_bin_dir)"
    case ":$PATH:" in
        *":$bindir:"*) ;;
        *) warn "add $bindir to PATH to use gr/us/dkb: echo 'export PATH=\"$bindir:\$PATH\"' >> ~/.zshrc" ;;
    esac
}

do_launchers() {
    launchers_active || return 0
    echo ""
    info "CLI launchers → $(launcher_bin_dir)"
    [[ -d "$SKILLS_DST/gost-report" ]] && write_launcher gr  "$(_gr_shim)"
    [[ -d "$SKILLS_DST/ultrasearch" ]] && write_launcher us  "$(_us_shim)"
    [[ -d "$SKILLS_DST/doc2kb" ]]      && write_launcher dkb "$(_dkb_shim)"
    [[ "${LAUNCHER_INSTALLED:-0}" -eq 1 ]] && launcher_path_notice
    return 0
}

do_launchers_remove() {
    launchers_active || return 0
    local bindir cmd target
    bindir="$(launcher_bin_dir)"
    for cmd in gr us dkb; do
        target="$bindir/$cmd"
        if [[ -f "$target" ]] && grep -q "$LAUNCHER_MARK" "$target" 2>/dev/null; then
            rm -f "$target"
            log "removed launcher $cmd"
        fi
    done
}

do_launchers_dry() {
    launchers_active || return 0
    local bindir cmd target found
    bindir="$(launcher_bin_dir)"
    echo "CLI launchers ($bindir):"
    for cmd in gr us dkb; do
        target="$bindir/$cmd"
        if found="$(command -v "$cmd" 2>/dev/null)"; then
            if [[ "$found" != "$target" ]] && ! grep -q "$LAUNCHER_MARK" "$found" 2>/dev/null; then
                warn "  ! $cmd (conflict: $found — would skip)"
            else
                echo "  = $cmd (update)"
            fi
        else
            info "  + $cmd (NEW)"
        fi
    done
    echo ""
}

# --- MinerU pre-warm (opt-in, gated; both targets ship doc2kb) ---
#
# HARD RULE: heavy ML deps (MinerU's ~3 GB+ MLX/CUDA stack) are NEVER auto-
# installed or silently routed. Pre-warm fires only when explicitly requested
# (--with-mineru, or the god preset) AND an interactive y/N is answered yes.
# Non-interactive shells (CI, piped installs) skip with a pointer. The
# lightweight tier stays the only thing that installs without confirmation.

mineru_prewarm_active() {
    [[ "$MINERU_PREWARM" -eq 1 && -n "$SKILLS_DST" ]]
}

do_mineru_prewarm() {
    mineru_prewarm_active || return 0
    local ensure="$SKILLS_DST/doc2kb/scripts/ensure_env.py"
    if [[ ! -f "$ensure" ]]; then
        warn "MinerU pre-warm skipped — doc2kb skill not installed this run"
        return 0
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        warn "MinerU pre-warm skipped — python3 not found"
        return 0
    fi
    if [[ ! -t 0 ]]; then
        warn "MinerU pre-warm skipped (non-interactive shell)."
        info "  run later: python3 \"$ensure\" --tier mineru"
        return 0
    fi
    echo ""
    warn "MinerU tier is a HEAVY download: ~3 GB+, several minutes (MLX/CUDA wheels)."
    printf "  Pre-warm doc2kb MinerU tier now? [y/N] "
    local reply=""
    read -r reply || true
    case "$reply" in
        y|Y|yes|YES)
            info "Installing MinerU tier — this will take a while..."
            if python3 "$ensure" --tier mineru; then
                log "MinerU tier installed"
            else
                warn "MinerU tier install failed — run later: python3 \"$ensure\" --tier mineru"
            fi
            ;;
        *)
            info "MinerU pre-warm declined — lightweight tier remains the default."
            info "  install later: python3 \"$ensure\" --tier mineru"
            ;;
    esac
}

do_mineru_prewarm_dry() {
    mineru_prewarm_active || return 0
    echo "MinerU pre-warm:"
    info "  + would pre-warm doc2kb MinerU tier (~3 GB, several minutes) — interactive y/N confirm at real install"
    echo ""
}

# --- caveman (third-party, opt-in, gated; god) ---
#
# Pipes a remote install script to bash (executes third-party code at install
# time). Same hard gate as MinerU: explicit request (--with-caveman / god) AND an
# interactive y/N (default N). Non-interactive shells skip. URL + source shown so
# the confirm is honest; the script is unpinned (main branch).

CAVEMAN_INSTALL_URL='https://raw.githubusercontent.com/JuliusBrussee/caveman/main/install.sh'

caveman_active() {
    [[ "$TARGET" == "claude" && "$CAVEMAN" -eq 1 ]]
}

do_caveman() {
    caveman_active || return 0
    if ! command -v curl >/dev/null 2>&1; then
        warn "caveman skipped — curl not found"
        return 0
    fi
    if [[ ! -t 0 ]]; then
        warn "caveman install skipped (non-interactive shell)."
        info "  run later: curl -fsSL $CAVEMAN_INSTALL_URL | bash"
        return 0
    fi
    echo ""
    warn "caveman is THIRD-PARTY. This pipes a remote script to bash (runs code):"
    warn "  $CAVEMAN_INSTALL_URL"
    warn "  Source: github.com/JuliusBrussee/caveman (main, unpinned). Needs node>=18."
    printf "  Install caveman now? [y/N] "
    local reply=""
    read -r reply || true
    case "$reply" in
        y|Y|yes|YES)
            info "Installing caveman..."
            if curl -fsSL "$CAVEMAN_INSTALL_URL" | bash; then
                log "caveman installed"
            else
                warn "caveman install failed — run later: curl -fsSL $CAVEMAN_INSTALL_URL | bash"
            fi
            ;;
        *)
            info "caveman declined."
            info "  install later: curl -fsSL $CAVEMAN_INSTALL_URL | bash"
            ;;
    esac
}

do_caveman_dry() {
    caveman_active || return 0
    echo "caveman (third-party):"
    info "  + would offer caveman via curl|bash ($CAVEMAN_INSTALL_URL) — interactive y/N at real install"
    echo ""
}

# --- Preset manifest + codex-downgrade notice ---
# Surfaces the resolved per-layer state so the bundle is obvious (cures the
# "I don't remember what's default" problem). Each line reflects the *active*
# state — i.e. it already accounts for target rules and --skills-only.

_mstate() { if "$@" >/dev/null 2>&1; then printf 'on'; else printf 'off'; fi; }

print_preset_manifest() {
    [[ -n "$PRESET" ]] || return 0
    echo ""
    info "Preset '$PRESET' resolved → target=$TARGET, model-profile=$MODEL_PROFILE"
    [[ "$SKILLS_ONLY" -eq 1 ]] && echo "    mode:                skills-only (agents/commands/settings layers off)"
    echo "    attribution-fix:     $(_mstate attribution_active)"
    echo "    config-defaults:     $(_mstate config_defaults_active)"
    echo "    claude-md baseline:  $(_mstate claude_md_active)"
    echo "    gost persona config: $(_mstate gost_config_active)"
    echo "    gost-validation:     $(_mstate gost_validation_active)"
    echo "    launchers gr/us/dkb: $(_mstate launchers_active)"
    echo "    stop sound:          $(_mstate stop_sound_active)"
    echo "    notification sound:  $(_mstate notification_sound_active)"
    echo "    thinking summaries:  $(_mstate thinking_summaries_active)"
    echo "    env defaults (xhigh):  $(_mstate env_defaults_active)"
    echo "    ccstatusline:        $(_mstate ccstatusline_active)"
    echo "    caveman (3rd-party): $(_mstate caveman_active)"
    echo "    MinerU pre-warm:     $(_mstate mineru_prewarm_active)"
}

preset_codex_downgrade_notice() {
    [[ "$TARGET" == "codex" ]] || return 0
    case "$PRESET" in
        minimum|default|senior|god)
            warn "Preset '$PRESET' targets Claude Code; under --target codex only"
            warn "  skills/gost-config/stop-sound/launchers apply. Use --preset codex-full."
            ;;
    esac
}

# --- Actions ---

do_install() {
    if [[ -n "$AGENTS_DST" ]]; then
        info "Installing agentpipe v$VERSION (target: $TARGET, model-profile: $MODEL_PROFILE) to: $BASE"
    else
        info "Installing agentpipe v$VERSION (target: $TARGET) to: $BASE"
    fi
    print_preset_manifest
    preset_codex_downgrade_notice
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

    if [[ -n "$SKILLS_DST" && -d "$SKILLS_SRC" ]]; then
        mkdir -p "$SKILLS_DST"
        for d in "$SKILLS_SRC"/*/; do
            [[ -d "$d" ]] || continue
            local name
            name=$(basename "$d")
            # ADR-008: move any legacy in-skill durable state (e.g. ultrasearch
            # corpus.db) out to the global state dir BEFORE removing the old code.
            # The freshly-shipped ensure_env owns the move (no-op for skills with
            # no durable data); runtime migration alone loses the race with this rm.
            if [[ -d "$SKILLS_DST/$name" ]] && command -v python3 >/dev/null 2>&1; then
                python3 "$d/scripts/ensure_env.py" --migrate-from "$SKILLS_DST/$name" >/dev/null 2>&1 || true
            fi
            rm -rf "$SKILLS_DST/$name"
            cp -R "$d" "$SKILLS_DST/$name"
            # Strip dev cruft a source checkout carries — the venv and any runtime
            # data (a dev clone may hold a corpus/cache); these must never ship.
            # Mirrors the exclusions in scripts/build-skills.sh. Runtime state
            # lives in the global state dir now (ADR-008).
            rm -rf "$SKILLS_DST/$name/.venv" "$SKILLS_DST/$name/.venv.lock" \
                   "$SKILLS_DST/$name/data/corpus.db" \
                   "$SKILLS_DST/$name/data/corpus.db-wal" \
                   "$SKILLS_DST/$name/data/corpus.db-shm" \
                   "$SKILLS_DST/$name/data/cache" \
                   "$SKILLS_DST/$name/data/retraction_watch.csv" \
                   "$SKILLS_DST/$name/data/retraction_watch.csv.tmp" \
                   "$SKILLS_DST/$name/data/_logs"
            log "skills/$name/"
            count=$((count + 1))
        done
    fi

    cleanup_legacy_codex_skills

    if attribution_active; then
        echo ""
        do_attribution_fix
    fi

    if config_defaults_active; then
        echo ""
        do_config_defaults
    fi

    if env_defaults_active; then
        echo ""
        do_env_defaults
    fi

    if ccstatusline_active; then
        echo ""
        do_ccstatusline
    fi

    if claude_md_active; then
        echo ""
        do_claude_md
    fi

    if gost_config_active; then
        echo ""
        do_gost_config
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

    do_launchers

    do_mineru_prewarm
    do_caveman

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
        # Skill runtime state (venvs, ultrasearch corpus) lives in a global dir
        # outside the code tree (ADR-008) and is shared across install targets,
        # so uninstall leaves it untouched. Point the user at it.
        local state_root="${AGENTPIPE_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/agentpipe}"
        if [[ -d "$state_root" ]]; then
            info "skill state preserved (shared across targets): $state_root"
            info "  remove manually if no longer needed (deletes venvs + ultrasearch corpus)"
        fi
    fi

    cleanup_legacy_codex_skills
    count=$((count + ${LEGACY_CODEX_CLEANED_COUNT:-0}))

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

    do_launchers_remove

    echo ""
    info "Removed $count items from $BASE"
}

do_dry() {
    info "Dry run (target: $TARGET) — would install to: $BASE"
    print_preset_manifest
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

    if [[ -n "$SKILLS_DST" && -d "$SKILLS_SRC" ]]; then
        echo "Skills ($SKILLS_DST):"
        for d in "$SKILLS_SRC"/*/; do
            [[ -d "$d" ]] || continue
            local name
            name=$(basename "$d")
            if [[ -d "$SKILLS_DST/$name" ]]; then
                if diff -rq -x .venv -x .venv.lock "$d" "$SKILLS_DST/$name" >/dev/null 2>&1; then
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

    dry_legacy_codex_cleanup

    do_launchers_dry
    do_attribution_dry
    do_config_defaults_dry
    do_env_defaults_dry
    do_ccstatusline_dry
    do_claude_md_dry
    do_gost_config_dry
    warn_sound_overlap
    do_stop_sound_hook_dry
    do_notification_sound_hook_dry
    do_thinking_summaries_dry
    do_gost_validation_dry
    do_mineru_prewarm_dry
    do_caveman_dry
    codex_skip_notice
    skills_only_notice
    preset_codex_downgrade_notice
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

    if [[ -n "$SKILLS_DST" && -d "$SKILLS_SRC" ]]; then
        for d in "$SKILLS_SRC"/*/; do
            [[ -d "$d" ]] || continue
            local name
            name=$(basename "$d")
            if [[ -d "$SKILLS_DST/$name" ]]; then
                if ! diff -rq -x .venv -x .venv.lock "$d" "$SKILLS_DST/$name" >/dev/null 2>&1; then
                    echo ""
                    warn "skills/$name/ differs:"
                    diff --color=auto -ru -x .venv -x .venv.lock "$SKILLS_DST/$name" "$d" || true
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

    if [[ -n "$SKILLS_DST" && -d "$SKILLS_DST" && -d "$SKILLS_SRC" ]]; then
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
    install)            do_install ;;
    dry)                do_dry ;;
    diff)               do_diff ;;
    pull)               do_pull ;;
    update)             do_update ;;
    uninstall)          do_uninstall ;;
    clean-sound-hooks)  do_clean_sound_hooks ;;
esac
