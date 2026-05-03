#Requires -Version 5.1
<#
.SYNOPSIS
    agentpipe — Install Script (Windows PowerShell)
.DESCRIPTION
    Copies agents, commands, and skills from this repo to ~/.claude/ (Claude Code, default)
    or ~/.agents/skills/ plus ~/.codex/skills/ (Codex CLI, with -Target codex; agents and commands are skipped
    because Codex agents use a different TOML format and Codex CLI has no custom slash commands).
.EXAMPLE
    .\install.ps1                          # install for Claude Code (default)
    .\install.ps1 -Target codex            # install for Codex CLI (skills only)
    .\install.ps1 -Dry                     # preview what would be copied
    .\install.ps1 -Diff                    # show repo vs installed differences
    .\install.ps1 -Pull                    # copy installed back to repo
    .\install.ps1 -Update                  # git pull --ff-only, then install
    .\install.ps1 -Uninstall               # remove installed files
    .\install.ps1 -CleanSoundHooks         # strip Stop+Notification sound hooks from settings.json
    .\install.ps1 -NoAttributionFix        # skip Co-Authored-By suppression layer
    .\install.ps1 -NoConfigDefaults        # skip $schema + safe defaults + deny list
    .\install.ps1 -NoClaudeMd              # skip neutral CLAUDE.md baseline (install-if-missing)
    .\install.ps1 -NoGostValidation        # skip gost-report Stop-hook validator (default: on)
    .\install.ps1 -SkillsOnly              # copy only skills/* (skip agents, commands, hooks)
    .\install.ps1 -WithSoundHooks          # opt-in: Stop sound hook only (one beep per turn)
    .\install.ps1 -WithNotificationSound   # opt-in: Notification sound hook only
    .\install.ps1 -WithThinkingSummaries   # opt-in: showThinkingSummaries=true
    .\install.ps1 -ModelProfile opus       # all agents on opus (default: mixed)
    .\install.ps1 -ShowVersion             # show version
#>
param(
    [ValidateSet("claude", "codex")]
    [string]$Target = "claude",
    [switch]$Dry,
    [switch]$Diff,
    [switch]$Pull,
    [switch]$Update,
    [switch]$Uninstall,
    [switch]$CleanSoundHooks,
    [switch]$NoAttributionFix,
    [switch]$NoConfigDefaults,
    [switch]$NoClaudeMd,
    [switch]$NoGostValidation,
    [switch]$SkillsOnly,
    [switch]$WithSoundHooks,
    [switch]$WithNotificationSound,
    [switch]$WithThinkingSummaries,
    # Validated manually below — ValidateSet rejects the empty default.
    [string]$ModelProfile = "",
    [switch]$ShowVersion,
    [switch]$Help
)

$ErrorActionPreference = "Stop"

$ScriptDir = $PSScriptRoot
$VersionFile = Join-Path $ScriptDir "VERSION"
$Script:Version = if (Test-Path $VersionFile) { (Get-Content $VersionFile -Raw).Trim() } else { "unknown" }

$AgentsSrc = Join-Path $ScriptDir "agents"
$CommandsSrc = Join-Path $ScriptDir "commands"
$SkillsSrc = Join-Path $ScriptDir "skills"
$HookSrc = Join-Path $ScriptDir "scripts/git-hooks/commit-msg"
$ClaudeMdSrc = Join-Path $ScriptDir "scripts/CLAUDE.md.example"
$GitTemplateDir = Join-Path $env:USERPROFILE ".git-templates"
$GitHookDst = Join-Path $GitTemplateDir "hooks/commit-msg"
$ConfigSchemaUrl = "https://json.schemastore.org/claude-code-settings.json"
# permissions.deny: secrets + universally-destructive Bash patterns.
$ConfigDenyList = @(
    "Read(./.env)"
    "Read(./.env.*)"
    "Read(./**/secrets/**)"
    "Read(./**/*.pem)"
    "Read(./**/*.key)"
    "Bash(rm -rf /*)"
    "Bash(rm -rf ~/*)"
    "Bash(rm -rf `$HOME/*)"
    "Bash(mkfs *)"
    "Bash(dd * of=/dev/*)"
)

# Resolve destinations from target. Codex skills go to ~/.agents/skills/ (open-agent-skills
# standard) and ~/.codex/skills/ (path loaded by current Codex sessions).
$SkillsExtraDst = $null
switch ($Target) {
    "claude" {
        $Base = Join-Path $env:USERPROFILE ".claude"
        $AgentsDst   = Join-Path $Base "agents"
        $CommandsDst = Join-Path $Base "commands"
        $SkillsDst   = Join-Path $Base "skills"
    }
    "codex" {
        $Base = Join-Path $env:USERPROFILE ".agents"
        $CodexBase = Join-Path $env:USERPROFILE ".codex"
        $AgentsDst   = $null
        $CommandsDst = $null
        $SkillsDst   = Join-Path $Base "skills"
        $SkillsExtraDst = Join-Path $CodexBase "skills"
    }
}

# -SkillsOnly: drop everything except the skills copy. Mirrors install.sh —
# null out the agent + command destinations (every action gates on those) and
# turn off every feature-flag layer. Composes with both -Target claude and
# -Target codex.
if ($SkillsOnly) {
    $AgentsDst = $null
    $CommandsDst = $null
    $NoAttributionFix = $true
    $NoConfigDefaults = $true
    $NoClaudeMd = $true
    $NoGostValidation = $true
    $WithSoundHooks = $false
    $WithNotificationSound = $false
    $WithThinkingSummaries = $false
}

function Write-Ok($msg)   { Write-Host "  $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  $msg" -ForegroundColor Yellow }
function Write-Info($msg) { Write-Host "  $msg" -ForegroundColor Cyan }
function Write-Err($msg)  { Write-Host "  $msg" -ForegroundColor Red }

function Show-CodexSkipNotice {
    if ($Target -eq "codex") {
        Write-Warn "Codex CLI has no custom slash commands - skipped commands/"
        Write-Warn "Codex agents use a different TOML format - skipped agents/. See README for details."
        Write-Info "Codex skills installed to both ~/.agents/skills/ and ~/.codex/skills/ for compatibility."
    }
}

function Show-SkillsOnlyNotice {
    if ($SkillsOnly -and $Target -eq "claude") {
        Write-Warn "-SkillsOnly - skipped agents/, commands/, and all settings.json layers"
    }
}

# --- Attribution-fix layer (claude target only) ---
# Mirrors install.sh: settings.json/includeCoAuthoredBy=false +
# global commit-msg hook + init.templateDir. Codex target skips both.

function Test-AttributionActive {
    return ($Target -eq "claude" -and -not $NoAttributionFix)
}

function Test-ConfigDefaultsActive {
    return ($Target -eq "claude" -and -not $NoConfigDefaults)
}

function Test-ClaudeMdActive {
    return ($Target -eq "claude" -and -not $NoClaudeMd)
}

function Test-StopSoundActive {
    return ($Target -eq "claude" -and $WithSoundHooks)
}

function Test-NotificationSoundActive {
    return ($Target -eq "claude" -and $WithNotificationSound)
}

function Test-ThinkingSummariesActive {
    return ($Target -eq "claude" -and $WithThinkingSummaries)
}

function Test-GostValidationActive {
    return ($Target -eq "claude" -and -not $NoGostValidation)
}

function Files-Equal($a, $b) {
    if (-not (Test-Path $a) -or -not (Test-Path $b)) { return $false }
    return (Get-FileHash $a).Hash -eq (Get-FileHash $b).Hash
}

function Read-SettingsJson {
    # Returns @{Hash=<hashtable>; Ok=$true} or @{Ok=$false} on parse error.
    $settings = Join-Path $Base "settings.json"
    $base = @{}
    if (Test-Path $settings) {
        try {
            $raw = (Get-Content $settings -Raw -ErrorAction Stop)
            if ($raw.Trim()) {
                $parsed = $raw | ConvertFrom-Json -ErrorAction Stop
                $parsed.PSObject.Properties | ForEach-Object {
                    $base[$_.Name] = $_.Value
                }
            }
        } catch {
            return @{ Ok = $false }
        }
    }
    return @{ Hash = $base; Ok = $true }
}

function Write-SettingsJson($base) {
    $settings = Join-Path $Base "settings.json"
    New-Item -ItemType Directory -Path $Base -Force | Out-Null
    $tmp = "$settings.agentpipe.tmp"
    try {
        ($base | ConvertTo-Json -Depth 32) | Set-Content -Path $tmp -Encoding UTF8 -NoNewline
        Add-Content -Path $tmp -Value "" -Encoding UTF8
        Move-Item -Path $tmp -Destination $settings -Force
        return $true
    } catch {
        if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
        Write-Warn "settings.json write failed: $_"
        return $false
    }
}

function Merge-SettingsJson {
    # Writes the modern attribution key (commit/pr=hidden) plus legacy
    # includeCoAuthoredBy=false for backward compat with older Claude Code.
    $r = Read-SettingsJson
    if (-not $r.Ok) {
        Write-Warn "settings.json has invalid JSON - leaving file untouched"
        return $false
    }
    $base = $r.Hash

    $base["attribution"] = @{ commit = ""; pr = "" }
    $base["includeCoAuthoredBy"] = $false

    if (Write-SettingsJson $base) {
        Write-Ok "settings/attribution=hidden + includeCoAuthoredBy=false"
        return $true
    }
    return $false
}

function Do-AttributionFix {
    if (-not (Test-AttributionActive)) { return }

    # 1. settings.json
    Merge-SettingsJson | Out-Null

    # 2. Global commit-msg hook
    New-Item -ItemType Directory -Path (Join-Path $GitTemplateDir "hooks") -Force | Out-Null
    if ((Test-Path $GitHookDst) -and (Files-Equal $HookSrc $GitHookDst)) {
        Write-Ok "git/commit-msg already current"
    } else {
        if (Test-Path $GitHookDst) {
            $epoch = [int][double]::Parse((Get-Date -UFormat %s))
            $backup = "$GitHookDst.agentpipe.bak.$epoch"
            Move-Item -Path $GitHookDst -Destination $backup
            Write-Warn "existing commit-msg hook backed up to $backup"
        }
        # Byte-for-byte copy preserves LF line endings (shebang needs LF on WSL).
        [System.IO.File]::WriteAllBytes($GitHookDst, [System.IO.File]::ReadAllBytes($HookSrc))
        Write-Ok "git/commit-msg installed -> $GitHookDst"
    }

    # 3. init.templateDir
    $current = (& git config --global --get init.templateDir 2>$null)
    if ($LASTEXITCODE -ne 0) { $current = "" }
    $currentExpanded = $current -replace '^~', $env:USERPROFILE
    if (-not $current) {
        & git config --global init.templateDir $GitTemplateDir
        Write-Ok "git/init.templateDir=$GitTemplateDir"
    } elseif ($currentExpanded -eq $GitTemplateDir) {
        Write-Ok "git/init.templateDir already set"
    } else {
        Write-Warn "init.templateDir already set to: $current"
        Write-Warn "  -> not overriding. Copy $GitHookDst into $current/hooks/ manually."
    }

    Write-Info "note: existing repos are unaffected - run 'git init' inside any repo"
    Write-Info "      to apply the hook, or copy the hook into .git/hooks/ manually."
}

function Do-AttributionUnfix {
    if (-not (Test-AttributionActive)) { return }

    if ((Test-Path $GitHookDst) -and (Files-Equal $HookSrc $GitHookDst)) {
        Remove-Item $GitHookDst
        Write-Ok "removed git/commit-msg"
    }

    $current = (& git config --global --get init.templateDir 2>$null)
    if ($LASTEXITCODE -ne 0) { $current = "" }
    $currentExpanded = $current -replace '^~', $env:USERPROFILE
    if ($currentExpanded -eq $GitTemplateDir) {
        & git config --global --unset init.templateDir
        Write-Ok "unset git/init.templateDir"
    }

    Write-Info "note: settings.json/includeCoAuthoredBy left as-is - edit manually to revert"
}

function Do-AttributionDry {
    if (-not (Test-AttributionActive)) { return }
    Write-Host "Attribution-fix:"
    $settings = Join-Path $Base "settings.json"
    if ((Test-Path $settings) -and (Select-String -Path $settings -Pattern '"includeCoAuthoredBy"\s*:\s*false' -Quiet)) {
        Write-Host "  = settings/includeCoAuthoredBy=false (already set)"
    } else {
        Write-Info "+ settings/includeCoAuthoredBy=false"
    }
    if ((Test-Path $GitHookDst) -and (Files-Equal $HookSrc $GitHookDst)) {
        Write-Host "  = git/commit-msg (identical)"
    } elseif (Test-Path $GitHookDst) {
        Write-Warn "~ git/commit-msg (CHANGED - existing hook will be backed up)"
    } else {
        Write-Info "+ git/commit-msg (NEW)"
    }
    $current = (& git config --global --get init.templateDir 2>$null)
    if ($LASTEXITCODE -ne 0) { $current = "" }
    $currentExpanded = $current -replace '^~', $env:USERPROFILE
    if ($currentExpanded -eq $GitTemplateDir) {
        Write-Host "  = git/init.templateDir=$GitTemplateDir"
    } elseif (-not $current) {
        Write-Info "+ git/init.templateDir=$GitTemplateDir"
    } else {
        Write-Warn "! git/init.templateDir already set to $current - will not override"
    }
    Write-Host ""
}

function Do-AttributionDiff {
    if (-not (Test-AttributionActive)) { return $true }
    if (-not (Test-Path $GitHookDst)) {
        Write-Warn "git-hooks/commit-msg - not installed"
        return $false
    }
    if (-not (Files-Equal $HookSrc $GitHookDst)) {
        Write-Warn "git-hooks/commit-msg differs"
        return $false
    }
    return $true
}

# --- Config-defaults layer (claude target only) ---
# $schema URL for IDE autocomplete + permissions.deny set-union for universal
# secret-file paths. User entries are preserved (set-union, not overwrite).

function Do-ConfigDefaults {
    if (-not (Test-ConfigDefaultsActive)) { return }

    $r = Read-SettingsJson
    if (-not $r.Ok) {
        Write-Warn "settings.json has invalid JSON - skipping config-defaults"
        return
    }
    $base = $r.Hash

    # Top-level scalars (overwrite is fine — these are universal defaults)
    $base["`$schema"] = $ConfigSchemaUrl
    $base["autoUpdatesChannel"] = "stable"
    $base["cleanupPeriodDays"] = 180
    $base["spinnerTipsEnabled"] = $false

    # permissions.deny set-union with user entries
    $perms = @{}
    if ($base.ContainsKey("permissions")) {
        $base["permissions"].PSObject.Properties | ForEach-Object {
            $perms[$_.Name] = $_.Value
        }
    }
    $existingDeny = @()
    if ($perms.ContainsKey("deny") -and $perms["deny"]) {
        $existingDeny = @($perms["deny"])
    }
    $unionDeny = [System.Collections.Generic.List[string]]::new()
    foreach ($item in $ConfigDenyList) {
        if (-not $unionDeny.Contains($item)) { $unionDeny.Add($item) }
    }
    foreach ($item in $existingDeny) {
        if (-not $unionDeny.Contains($item)) { $unionDeny.Add($item) }
    }
    $perms["deny"] = $unionDeny.ToArray()
    $base["permissions"] = $perms

    if (Write-SettingsJson $base) {
        Write-Ok "settings/config-defaults merged (`$schema + autoUpdatesChannel + cleanupPeriodDays + spinnerTipsEnabled + permissions.deny)"
    }
}

function Do-ConfigDefaultsUnfix {
    if (-not (Test-ConfigDefaultsActive)) { return }
    Write-Info "note: `$schema + permissions.deny left as-is - edit settings.json to revert"
}

function Do-ConfigDefaultsDry {
    if (-not (Test-ConfigDefaultsActive)) { return }
    Write-Host "Config-defaults:"
    $settings = Join-Path $Base "settings.json"
    $matchers = @(
        @{ Label = "`$schema=$ConfigSchemaUrl"; Pattern = $ConfigSchemaUrl }
        @{ Label = "autoUpdatesChannel=stable (vs default 'latest' beta)"; Pattern = '"autoUpdatesChannel": "stable"' }
        @{ Label = "cleanupPeriodDays=180 (vs default 30)"; Pattern = '"cleanupPeriodDays": 180' }
        @{ Label = "spinnerTipsEnabled=false"; Pattern = '"spinnerTipsEnabled": false' }
        @{ Label = "permissions.deny (secrets + destructive Bash)"; Pattern = 'Bash(rm -rf /*)' }
    )
    foreach ($m in $matchers) {
        if ((Test-Path $settings) -and (Select-String -Path $settings -SimpleMatch -Pattern $m.Pattern -Quiet)) {
            Write-Host "  = settings/$($m.Label) (already set)"
        } else {
            Write-Info "+ settings/$($m.Label)"
        }
    }
    Write-Host ""
}

# --- CLAUDE.md baseline (claude target only, install-if-missing) ---

function Do-ClaudeMd {
    if (-not (Test-ClaudeMdActive)) { return }
    $dst = Join-Path $Base "CLAUDE.md"
    if (Test-Path $dst) {
        Write-Ok "claude-md/CLAUDE.md already exists - not overwriting"
    } else {
        New-Item -ItemType Directory -Path $Base -Force | Out-Null
        Copy-Item -Path $ClaudeMdSrc -Destination $dst -Force
        Write-Ok "claude-md/CLAUDE.md installed (neutral baseline) -> $dst"
    }
}

function Do-ClaudeMdDry {
    if (-not (Test-ClaudeMdActive)) { return }
    Write-Host "Claude.md baseline:"
    $dst = Join-Path $Base "CLAUDE.md"
    if (Test-Path $dst) {
        Write-Host "  = CLAUDE.md (already exists, will not overwrite)"
    } else {
        Write-Info "+ CLAUDE.md (neutral baseline, install-if-missing)"
    }
    Write-Host ""
}

# --- Sound hooks (claude target only, opt-in) ---
# -WithSoundHooks         → Stop sound hook only (one beep when Claude finishes)
# -WithNotificationSound  → Notification sound hook only (permission/wait-for-input)
# Passing both is allowed; a warning is printed because Notification often
# fires right after Stop, producing two beeps in sequence.

# Sound-cue command pattern. Mirrors scripts/clean-sound-hooks.py — keep in sync.
$Script:SoundCmdPattern = '(?i)(\bafplay\b|\bpaplay\b|\[console\]::beep|powershell(\.exe)?\b[^|;&]*\bbeep\b)'

function Test-SoundCommand($cmd) {
    if (-not $cmd) { return $false }
    return [bool]([regex]::Match($cmd, $Script:SoundCmdPattern).Success)
}

function Merge-SoundHook($eventName, $cmd) {
    $r = Read-SettingsJson
    if (-not $r.Ok) {
        Write-Warn "settings.json has invalid JSON - skipping sound hook ($eventName)"
        return
    }
    $base = $r.Hash

    $hooks = @{}
    if ($base.ContainsKey("hooks")) {
        $base["hooks"].PSObject.Properties | ForEach-Object {
            $hooks[$_.Name] = $_.Value
        }
    }

    $entry = @{ hooks = @(@{ type = "command"; command = $cmd }) }
    $existing = @()
    if ($hooks.ContainsKey($eventName) -and $hooks[$eventName]) {
        $existing = @($hooks[$eventName])
    }
    # Append only if no existing entry has the exact same command
    $alreadyPresent = $false
    foreach ($e in $existing) {
        $existingCmds = @()
        if ($e.PSObject.Properties.Name -contains "hooks") {
            $existingCmds = @($e.hooks | ForEach-Object { $_.command })
        } elseif ($e -is [hashtable] -and $e.ContainsKey("hooks")) {
            $existingCmds = @($e["hooks"] | ForEach-Object { $_.command })
        }
        if ($existingCmds -contains $cmd) { $alreadyPresent = $true; break }
    }
    if (-not $alreadyPresent) {
        $hooks[$eventName] = @($entry) + $existing
    }
    $base["hooks"] = $hooks

    if (Write-SettingsJson $base) {
        Write-Ok "settings/hooks.$eventName (Windows beep) merged"
    }
}

function Do-StopSoundHook {
    if (-not (Test-StopSoundActive)) { return }
    Merge-SoundHook "Stop" "powershell -c [console]::beep(880,150)"
}

function Do-NotificationSoundHook {
    if (-not (Test-NotificationSoundActive)) { return }
    Merge-SoundHook "Notification" "powershell -c [console]::beep(660,250)"
}

function Warn-SoundOverlap {
    if ((Test-StopSoundActive) -and (Test-NotificationSoundActive)) {
        Write-Warn "-WithSoundHooks AND -WithNotificationSound both set:"
        Write-Warn "  Notification often fires right after Stop, so you may hear"
        Write-Warn "  two beeps in sequence at the end of each chat. Pass only one"
        Write-Warn "  flag if that's not intended, or run -CleanSoundHooks to reset."
    }
}

function Do-StopSoundHookDry {
    if (-not (Test-StopSoundActive)) { return }
    Write-Host "Sound hook (Stop):"
    Write-Info "+ settings/hooks.Stop (Windows beep)"
    Write-Host ""
}

function Do-NotificationSoundHookDry {
    if (-not (Test-NotificationSoundActive)) { return }
    Write-Host "Sound hook (Notification):"
    Write-Info "+ settings/hooks.Notification (Windows beep)"
    Write-Host ""
}

# -CleanSoundHooks action — strip every sound-hook entry (Stop+Notification)
# from settings.json. Recognises afplay, paplay, [console]::beep, powershell beep.
# Leaves non-sound hooks (gost-validation, user customs) intact.
function Do-CleanSoundHooks {
    if ($Target -ne "claude") {
        Write-Info "clean-sound-hooks: no-op for codex (Codex CLI has no hooks)"
        return
    }
    $settings = Join-Path $Base "settings.json"
    if (-not (Test-Path $settings)) {
        Write-Info "no settings.json at $settings - nothing to clean"
        return
    }
    $r = Read-SettingsJson
    if (-not $r.Ok) {
        Write-Err "settings.json has invalid JSON - cannot clean sound hooks"
        exit 1
    }
    $base = $r.Hash
    if (-not $base.ContainsKey("hooks")) {
        Write-Info "no hooks section in $settings - nothing to clean"
        return
    }

    $hooksObj = $base["hooks"]
    $hooks = @{}
    if ($hooksObj -is [hashtable]) {
        $hooksObj.GetEnumerator() | ForEach-Object { $hooks[$_.Key] = $_.Value }
    } else {
        $hooksObj.PSObject.Properties | ForEach-Object { $hooks[$_.Name] = $_.Value }
    }

    $totalRemoved = 0
    foreach ($eventName in @("Stop", "Notification")) {
        if (-not $hooks.ContainsKey($eventName)) { continue }
        $entries = @($hooks[$eventName])
        $newEntries = @()
        foreach ($entry in $entries) {
            $inner = @()
            $hasInner = $false
            if ($entry -is [hashtable] -and $entry.ContainsKey("hooks")) {
                $inner = @($entry["hooks"])
                $hasInner = $true
            } elseif ($entry.PSObject -and ($entry.PSObject.Properties.Name -contains "hooks")) {
                $inner = @($entry.hooks)
                $hasInner = $true
            }
            if (-not $hasInner) {
                $newEntries += $entry
                continue
            }
            $kept = @($inner | Where-Object { -not (Test-SoundCommand $_.command) })
            $totalRemoved += ($inner.Count - $kept.Count)
            if ($kept.Count -gt 0) {
                $newEntry = @{ hooks = $kept }
                if ($entry -is [hashtable]) {
                    foreach ($k in $entry.Keys) {
                        if ($k -ne "hooks") { $newEntry[$k] = $entry[$k] }
                    }
                } else {
                    $entry.PSObject.Properties | Where-Object { $_.Name -ne "hooks" } | ForEach-Object {
                        $newEntry[$_.Name] = $_.Value
                    }
                }
                $newEntries += $newEntry
            }
        }
        if ($newEntries.Count -gt 0) {
            $hooks[$eventName] = $newEntries
        } else {
            $hooks.Remove($eventName)
        }
    }

    if ($hooks.Count -eq 0) {
        $base.Remove("hooks")
    } else {
        $base["hooks"] = $hooks
    }

    if ($totalRemoved -gt 0) {
        if (Write-SettingsJson $base) {
            $word = if ($totalRemoved -eq 1) { "entry" } else { "entries" }
            Write-Ok "removed $totalRemoved sound hook $word from $settings"
        }
    } else {
        Write-Info "no sound hook entries found in $settings"
    }
}

# --- Thinking summaries (claude target only, opt-in) ---

function Do-ThinkingSummaries {
    if (-not (Test-ThinkingSummariesActive)) { return }
    $r = Read-SettingsJson
    if (-not $r.Ok) {
        Write-Warn "settings.json has invalid JSON - skipping showThinkingSummaries"
        return
    }
    $base = $r.Hash
    $base["showThinkingSummaries"] = $true
    if (Write-SettingsJson $base) {
        Write-Ok "settings/showThinkingSummaries=true"
    }
}

function Do-ThinkingSummariesDry {
    if (-not (Test-ThinkingSummariesActive)) { return }
    Write-Host "Thinking summaries:"
    Write-Info "+ settings/showThinkingSummaries=true"
    Write-Host ""
}

# --- gost-report validation hook (claude target only, default-on) ---
# Mirror of install.sh's do_gost_validation. See that block for rationale.
# Stop hook runs validate.py against any .docx with a sibling sentinel,
# emits {"decision":"block","reason":"..."} JSON on failure. Always exits 0.
# Sentinel-only scoping → no false fires in non-gost-report projects.
# Codex target skips this layer (Codex CLI has no hooks).

function Do-GostValidation {
    if (-not (Test-GostValidationActive)) { return }
    $validatePath = Join-Path $SkillsDst "gost-report\scripts\validate.py"
    if (-not (Test-Path $validatePath)) { return }

    $r = Read-SettingsJson
    if (-not $r.Ok) {
        Write-Warn "settings.json has invalid JSON - skipping gost-validation"
        return
    }
    $base = $r.Hash

    $hooks = @{}
    if ($base.ContainsKey("hooks")) {
        $base["hooks"].PSObject.Properties | ForEach-Object {
            $hooks[$_.Name] = $_.Value
        }
    }

    # Hook command: invoke python on validate.py. validate.py self-bootstraps
    # via skill venv if system python lacks python-docx (see _maybe_reexec_in_venv
    # in validate.py). Hook always exits 0 internally — never crashes Stop.
    $cmd = "python `"$validatePath`" --hook"

    $entry = @{ hooks = @(@{ type = "command"; command = $cmd }) }
    $existing = @()
    if ($hooks.ContainsKey("Stop") -and $hooks["Stop"]) {
        $existing = @($hooks["Stop"])
    }
    $alreadyPresent = $false
    foreach ($e in $existing) {
        $existingCmds = @()
        if ($e.PSObject.Properties.Name -contains "hooks") {
            $existingCmds = @($e.hooks | ForEach-Object { $_.command })
        } elseif ($e -is [hashtable] -and $e.ContainsKey("hooks")) {
            $existingCmds = @($e["hooks"] | ForEach-Object { $_.command })
        }
        if ($existingCmds -contains $cmd) { $alreadyPresent = $true; break }
    }
    if (-not $alreadyPresent) {
        $hooks["Stop"] = @($entry) + $existing
    }
    $base["hooks"] = $hooks

    if (Write-SettingsJson $base) {
        Write-Ok "settings/hooks.Stop += gost-report validate (deterministic, invisible to model)"
    }
}

function Do-GostValidationDry {
    if (-not (Test-GostValidationActive)) { return }
    Write-Host "Gost-report validation hook:"
    Write-Info "+ settings/hooks.Stop += gost-report validate (deterministic, default-on)"
    Write-Host ""
}

# --- Model-profile layer (claude target only) ---
# See install.sh's "Model-profile layer" comment for the design rationale.
# Three presets: opus, sonnet, mixed (default = canonical opus-for-architect+
# security, sonnet-for-the-rest). agents/*.md are NEVER modified — rewriting
# happens at copy time. Choice is persisted to settings.json/agentpipeModelProfile.

function Get-CanonicalModel($agentName) {
    if ($agentName -eq "architect" -or $agentName -eq "security") { return "opus" }
    return "sonnet"
}

function Get-ModelForProfile($profile, $agentName) {
    if ($profile -eq "opus" -or $profile -eq "sonnet") { return $profile }
    return (Get-CanonicalModel $agentName)
}

function Apply-ModelRewrite($srcPath, $dstPath, $profile) {
    $agentName = [System.IO.Path]::GetFileNameWithoutExtension($srcPath)
    $target = Get-ModelForProfile $profile $agentName
    $enc = New-Object System.Text.UTF8Encoding $false
    $content = [System.IO.File]::ReadAllText($srcPath, $enc)
    $newContent = [regex]::Replace($content, '(?m)^model: (opus|sonnet|haiku).*$', "model: $target")
    [System.IO.File]::WriteAllText($dstPath, $newContent, $enc)
}

function Read-PersistedProfile {
    $r = Read-SettingsJson
    if (-not $r.Ok) { return "" }
    if (-not $r.Hash.ContainsKey("agentpipeModelProfile")) { return "" }
    $v = [string]$r.Hash["agentpipeModelProfile"]
    if ($v -in @("opus", "sonnet", "mixed")) { return $v }
    return ""
}

function Persist-Profile($profile) {
    if ($Target -ne "claude") { return }
    $r = Read-SettingsJson
    if (-not $r.Ok) { return }
    $base = $r.Hash
    $base["agentpipeModelProfile"] = $profile
    Write-SettingsJson $base | Out-Null
}

# Resolve $ModelProfile: CLI flag > persisted (settings.json) > default 'mixed'.
$Script:ModelProfileFlag = $ModelProfile  # remember if user passed it (for persist gating)
if (-not $ModelProfile) {
    if ($Target -eq "claude") {
        $persisted = Read-PersistedProfile
        if ($persisted) { $ModelProfile = $persisted } else { $ModelProfile = "mixed" }
    } else {
        $ModelProfile = "mixed"
    }
}

if ($ModelProfile -notin @("opus", "sonnet", "mixed")) {
    Write-Err "Invalid -ModelProfile: $ModelProfile (use: opus, sonnet, mixed)"
    exit 1
}

function Do-Install {
    if ($AgentsDst) {
        Write-Info "Installing agentpipe v$($Script:Version) (target: $Target, model-profile: $ModelProfile) to: $Base"
    } else {
        Write-Info "Installing agentpipe v$($Script:Version) (target: $Target) to: $Base"
        if ($SkillsExtraDst) {
            Write-Info "Additional Codex skills path: $SkillsExtraDst"
        }
    }
    $count = 0

    if ($AgentsDst) {
        New-Item -ItemType Directory -Path $AgentsDst -Force | Out-Null
        Get-ChildItem "$AgentsSrc\*.md" -ErrorAction SilentlyContinue | ForEach-Object {
            Apply-ModelRewrite $_.FullName (Join-Path $AgentsDst $_.Name) $ModelProfile
            Write-Ok "agents/$($_.Name)"
            $count++
        }
    }

    if ($CommandsDst) {
        New-Item -ItemType Directory -Path $CommandsDst -Force | Out-Null
        Get-ChildItem "$CommandsSrc\*.md" -ErrorAction SilentlyContinue | ForEach-Object {
            Copy-Item $_.FullName -Destination (Join-Path $CommandsDst $_.Name) -Force
            Write-Ok "commands/$($_.Name)"
            $count++
        }
    }

    if (Test-Path $SkillsSrc) {
        foreach ($skillsDst in @($SkillsDst, $SkillsExtraDst)) {
            if (-not $skillsDst) { continue }
            New-Item -ItemType Directory -Path $skillsDst -Force | Out-Null
            Get-ChildItem $SkillsSrc -Directory | ForEach-Object {
                $dst = Join-Path $skillsDst $_.Name
                if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
                Copy-Item $_.FullName -Destination $dst -Recurse -Force
                # Скиллы могут держать свой venv в .venv/ (создаётся
                # bootstrap-скриптом). Не тащим dev venv в системную установку.
                Remove-Item -Path (Join-Path $dst ".venv") -Recurse -Force -ErrorAction SilentlyContinue
                Remove-Item -Path (Join-Path $dst ".venv.lock") -Force -ErrorAction SilentlyContinue
                Write-Ok "skills/$($_.Name)/ -> $dst"
                $count++
            }
        }
    }

    if (Test-AttributionActive) {
        Write-Host ""
        Do-AttributionFix
    }

    if (Test-ConfigDefaultsActive) {
        Write-Host ""
        Do-ConfigDefaults
    }

    if (Test-ClaudeMdActive) {
        Write-Host ""
        Do-ClaudeMd
    }

    Warn-SoundOverlap

    if (Test-StopSoundActive) {
        Write-Host ""
        Do-StopSoundHook
    }

    if (Test-NotificationSoundActive) {
        Write-Host ""
        Do-NotificationSoundHook
    }

    if (Test-ThinkingSummariesActive) {
        Write-Host ""
        Do-ThinkingSummaries
    }

    if (Test-GostValidationActive) {
        Write-Host ""
        Do-GostValidation
    }

    # Persist profile only when user explicitly passed -ModelProfile — implicit
    # defaults don't pollute settings.json. Skipped under -SkillsOnly: no agents
    # are touched, so the profile choice is meaningless for this run.
    if ($Script:ModelProfileFlag -and $Target -eq "claude" -and -not $SkillsOnly) {
        Persist-Profile $ModelProfile
    }

    Write-Host ""
    Write-Info "Installed $count items to $Base"
    Show-CodexSkipNotice
    Show-SkillsOnlyNotice
    Write-Ok "agentpipe v$($Script:Version)"
}

function Do-Uninstall {
    Write-Info "Uninstalling agentpipe from: $Base (target: $Target)"
    $count = 0

    if ($AgentsDst) {
        Get-ChildItem "$AgentsSrc\*.md" -ErrorAction SilentlyContinue | ForEach-Object {
            $dst = Join-Path $AgentsDst $_.Name
            if (Test-Path $dst) {
                Remove-Item $dst
                Write-Ok "removed agents/$($_.Name)"
                $count++
            }
        }
    }

    if ($CommandsDst) {
        Get-ChildItem "$CommandsSrc\*.md" -ErrorAction SilentlyContinue | ForEach-Object {
            $dst = Join-Path $CommandsDst $_.Name
            if (Test-Path $dst) {
                Remove-Item $dst
                Write-Ok "removed commands/$($_.Name)"
                $count++
            }
        }
    }

    if (Test-Path $SkillsSrc) {
        foreach ($skillsDst in @($SkillsDst, $SkillsExtraDst)) {
            if (-not $skillsDst) { continue }
            Get-ChildItem $SkillsSrc -Directory -ErrorAction SilentlyContinue | ForEach-Object {
                $dst = Join-Path $skillsDst $_.Name
                if (Test-Path $dst) {
                    Remove-Item $dst -Recurse -Force
                    Write-Ok "removed skills/$($_.Name)/ from $skillsDst"
                    $count++
                }
            }
        }
    }

    foreach ($d in @($AgentsDst, $CommandsDst, $SkillsDst, $SkillsExtraDst)) {
        if ($d -and (Test-Path $d) -and (@(Get-ChildItem $d).Count -eq 0)) {
            Remove-Item $d
            Write-Ok "removed $((Split-Path $d -Leaf))/"
        }
    }

    if (Test-AttributionActive) {
        Write-Host ""
        Do-AttributionUnfix
    }

    if (Test-ConfigDefaultsActive) {
        Write-Host ""
        Do-ConfigDefaultsUnfix
    }

    Write-Host ""
    Write-Info "Removed $count items from $Base"
}

function Do-Dry {
    Write-Info "Dry run (target: $Target) - would install to: $Base"
    Write-Host ""

    if ($AgentsDst) {
        Write-Host "Agents (model-profile: $ModelProfile):"
        $tmp = [System.IO.Path]::GetTempFileName()
        try {
            Get-ChildItem "$AgentsSrc\*.md" -ErrorAction SilentlyContinue | ForEach-Object {
                $dst = Join-Path $AgentsDst $_.Name
                Apply-ModelRewrite $_.FullName $tmp $ModelProfile
                if (Test-Path $dst) {
                    $srcHash = (Get-FileHash $tmp).Hash
                    $dstHash = (Get-FileHash $dst).Hash
                    if ($srcHash -eq $dstHash) {
                        Write-Host "  = $($_.Name) (identical)"
                    } else {
                        Write-Warn "~ $($_.Name) (CHANGED)"
                    }
                } else {
                    Write-Info "+ $($_.Name) (NEW)"
                }
            }
        } finally {
            if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
        }
        Write-Host ""
    }

    if ($CommandsDst) {
        Write-Host "Commands:"
        Get-ChildItem "$CommandsSrc\*.md" -ErrorAction SilentlyContinue | ForEach-Object {
            $dst = Join-Path $CommandsDst $_.Name
            if (Test-Path $dst) {
                $srcHash = (Get-FileHash $_.FullName).Hash
                $dstHash = (Get-FileHash $dst).Hash
                if ($srcHash -eq $dstHash) {
                    Write-Host "  = $($_.Name) (identical)"
                } else {
                    Write-Warn "~ $($_.Name) (CHANGED)"
                }
            } else {
                Write-Info "+ $($_.Name) (NEW)"
            }
        }
        Write-Host ""
    }

    if (Test-Path $SkillsSrc) {
        foreach ($skillsDst in @($SkillsDst, $SkillsExtraDst)) {
            if (-not $skillsDst) { continue }
            Write-Host "Skills ($skillsDst):"
            Get-ChildItem $SkillsSrc -Directory | ForEach-Object {
                $dst = Join-Path $skillsDst $_.Name
                if (Test-Path $dst) {
                    # Hash-based folder comparison: concatenate file hashes
                    $srcFiles = Get-ChildItem $_.FullName -Recurse -File | Sort-Object FullName
                    $dstFiles = Get-ChildItem $dst -Recurse -File | Sort-Object FullName
                    $srcSig = ($srcFiles | ForEach-Object { (Get-FileHash $_.FullName).Hash }) -join ""
                    $dstSig = ($dstFiles | ForEach-Object { (Get-FileHash $_.FullName).Hash }) -join ""
                    if ($srcSig -eq $dstSig -and $srcFiles.Count -eq $dstFiles.Count) {
                        Write-Host "  = $($_.Name)/ (identical)"
                    } else {
                        Write-Warn "~ $($_.Name)/ (CHANGED)"
                    }
                } else {
                    Write-Info "+ $($_.Name)/ (NEW)"
                }
            }
            Write-Host ""
        }
    }

    Do-AttributionDry
    Do-ConfigDefaultsDry
    Do-ClaudeMdDry
    Warn-SoundOverlap
    Do-StopSoundHookDry
    Do-NotificationSoundHookDry
    Do-ThinkingSummariesDry
    Do-GostValidationDry
    Show-CodexSkipNotice
    Show-SkillsOnlyNotice
}

function Do-Diff {
    Write-Info "Comparing repo <-> installed at $Base (target: $Target)"
    $hasDiff = $false

    $pairs = @()
    if ($AgentsDst)   { $pairs += @{ Label = "agents"; Src = $AgentsSrc; Dst = $AgentsDst; Rewrite = $true } }
    if ($CommandsDst) { $pairs += @{ Label = "commands"; Src = $CommandsSrc; Dst = $CommandsDst; Rewrite = $false } }

    foreach ($pair in $pairs) {
        Get-ChildItem "$($pair.Src)\*.md" -ErrorAction SilentlyContinue | ForEach-Object {
            $dstFile = Join-Path $pair.Dst $_.Name
            if (Test-Path $dstFile) {
                if ($pair.Rewrite) {
                    $tmp = [System.IO.Path]::GetTempFileName()
                    try {
                        Apply-ModelRewrite $_.FullName $tmp $ModelProfile
                        $srcContent = Get-Content $tmp -Raw
                    } finally {
                        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
                    }
                } else {
                    $srcContent = Get-Content $_.FullName -Raw
                }
                $dstContent = Get-Content $dstFile -Raw
                if ($srcContent -ne $dstContent) {
                    Write-Warn "$($pair.Label)/$($_.Name) differs"
                    $hasDiff = $true
                }
            } else {
                Write-Warn "$($pair.Label)/$($_.Name) - not installed"
                $hasDiff = $true
            }
        }
    }

    if (Test-Path $SkillsSrc) {
        foreach ($skillsDst in @($SkillsDst, $SkillsExtraDst)) {
            if (-not $skillsDst) { continue }
            Get-ChildItem $SkillsSrc -Directory | ForEach-Object {
                $dst = Join-Path $skillsDst $_.Name
                if (Test-Path $dst) {
                    $srcFiles = Get-ChildItem $_.FullName -Recurse -File | Sort-Object FullName
                    $dstFiles = Get-ChildItem $dst -Recurse -File | Sort-Object FullName
                    $srcSig = ($srcFiles | ForEach-Object { (Get-FileHash $_.FullName).Hash }) -join ""
                    $dstSig = ($dstFiles | ForEach-Object { (Get-FileHash $_.FullName).Hash }) -join ""
                    if ($srcSig -ne $dstSig -or $srcFiles.Count -ne $dstFiles.Count) {
                        Write-Warn "skills/$($_.Name)/ differs at $skillsDst"
                        $hasDiff = $true
                    }
                } else {
                    Write-Warn "skills/$($_.Name)/ - not installed at $skillsDst"
                    $hasDiff = $true
                }
            }
        }
    }

    if (Test-AttributionActive) {
        if (-not (Do-AttributionDiff)) {
            $hasDiff = $true
        }
    }

    if (-not $hasDiff) {
        Write-Ok "Everything in sync"
    }
}

function Do-Update {
    Write-Info "Updating agentpipe from remote, then installing..."

    Push-Location $ScriptDir
    try {
        & git rev-parse --is-inside-work-tree 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Err "$ScriptDir is not a git repository - can't pull."
            Write-Err "Re-clone the repo or download a fresh release zip."
            exit 1
        }

        $status = & git status --porcelain
        if ($status) {
            Write-Err "Working tree has uncommitted changes. Stash or commit them, then re-run."
            & git status --short
            exit 1
        }

        Write-Info "git pull --ff-only"
        & git pull --ff-only
        if ($LASTEXITCODE -ne 0) {
            Write-Err "git pull --ff-only failed (probably divergent history)."
            Write-Err "Resolve manually (rebase / merge / reset --hard origin/main) and re-run."
            exit 1
        }

        # VERSION may have changed in the pulled commits.
        $Script:Version = if (Test-Path (Join-Path $ScriptDir "VERSION")) {
            (Get-Content (Join-Path $ScriptDir "VERSION") -Raw).Trim()
        } else { "unknown" }
    }
    finally {
        Pop-Location
    }

    Write-Host ""
    Do-Install
}

function Do-Pull {
    Write-Info "Pulling installed versions back to repo (target: $Target)"
    $count = 0

    if ($AgentsDst -and (Test-Path $AgentsDst)) {
        # Strip user-side profile rewrite back to canonical mixed defaults so the
        # repo source-of-truth never gets contaminated by an installed all-opus
        # or all-sonnet copy.
        $stripped = $false
        Get-ChildItem "$AgentsDst\*.md" -ErrorAction SilentlyContinue | ForEach-Object {
            Apply-ModelRewrite $_.FullName (Join-Path $AgentsSrc $_.Name) "mixed"
            Write-Ok "agents/$($_.Name) <- installed"
            $count++
            $stripped = $true
        }
        if ($stripped -and $ModelProfile -ne "mixed") {
            Write-Info "pulled back to canonical mixed defaults - installed profile was $ModelProfile"
        }
    }

    if ($CommandsDst -and (Test-Path $CommandsDst)) {
        Get-ChildItem "$CommandsDst\*.md" -ErrorAction SilentlyContinue | ForEach-Object {
            Copy-Item $_.FullName -Destination (Join-Path $CommandsSrc $_.Name) -Force
            Write-Ok "commands/$($_.Name) <- installed"
            $count++
        }
    }

    $pullSkillsDst = $SkillsDst
    if ((-not (Test-Path $pullSkillsDst)) -and $SkillsExtraDst) {
        $pullSkillsDst = $SkillsExtraDst
    }

    if ($pullSkillsDst -and (Test-Path $pullSkillsDst) -and (Test-Path $SkillsSrc)) {
        Get-ChildItem $SkillsSrc -Directory | ForEach-Object {
            $dst = Join-Path $pullSkillsDst $_.Name
            if (Test-Path $dst) {
                $repoCopy = Join-Path $SkillsSrc $_.Name
                if (Test-Path $repoCopy) { Remove-Item $repoCopy -Recurse -Force }
                Copy-Item $dst -Destination $repoCopy -Recurse -Force
                Remove-Item -Path (Join-Path $repoCopy ".venv") -Recurse -Force -ErrorAction SilentlyContinue
                Remove-Item -Path (Join-Path $repoCopy ".venv.lock") -Force -ErrorAction SilentlyContinue
                Write-Ok "skills/$($_.Name)/ <- installed from $pullSkillsDst"
                $count++
            }
        }
    }

    Write-Host ""
    Write-Info "Pulled $count items into repo"
}

# Main
if ($Help) {
    Get-Help $MyInvocation.MyCommand.Path -Detailed
    exit 0
}

if ($ShowVersion) {
    Write-Host "agentpipe v$($Script:Version)"
    exit 0
}

if ($Dry) { Do-Dry }
elseif ($Diff) { Do-Diff }
elseif ($Pull) { Do-Pull }
elseif ($Update) { Do-Update }
elseif ($Uninstall) { Do-Uninstall }
elseif ($CleanSoundHooks) { Do-CleanSoundHooks }
else { Do-Install }
