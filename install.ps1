#Requires -Version 5.1
<#
.SYNOPSIS
    agentpipe — Install Script (Windows PowerShell)
.DESCRIPTION
    Copies agents, commands, and skills from this repo to ~/.claude/ (Claude Code, default)
    or ~/.codex/skills/ (Codex CLI, with -Target codex; agents and commands are skipped
    because Codex agents use a different TOML format and Codex CLI has no custom slash commands).
.EXAMPLE
    .\install.ps1                          # install for Claude Code (default)
    .\install.ps1 -Target codex            # install for Codex CLI (skills only)
    .\install.ps1 -Dry                     # preview what would be copied
    .\install.ps1 -Diff                    # show repo vs installed differences
    .\install.ps1 -Pull                    # copy installed back to repo
    .\install.ps1 -Update                  # git pull --ff-only, then install
    .\install.ps1 -Uninstall               # remove installed files
    .\install.ps1 -CleanSoundHooks         # strip Stop+Notification sound hooks from settings/hooks config
    .\install.ps1 -NoAttributionFix        # skip Co-Authored-By suppression layer
    .\install.ps1 -NoConfigDefaults        # skip $schema + safe defaults + deny list
    .\install.ps1 -NoClaudeMd              # skip neutral CLAUDE.md baseline (install-if-missing)
    .\install.ps1 -NoGostValidation        # skip gost-report Stop-hook validator (default: on)
    .\install.ps1 -SkillsOnly              # copy only skills/* (skip agents, commands, hooks)
    .\install.ps1 -NoLaunchers             # skip installing gr/us/dkb CLI launchers onto PATH
    .\install.ps1 -WithSoundHooks          # opt-in: Stop sound hook only (one beep per turn)
    .\install.ps1 -WithNotificationSound   # opt-in: Claude Notification sound hook only
    .\install.ps1 -WithThinkingSummaries   # opt-in: showThinkingSummaries=true
    .\install.ps1 -Preset god              # bundle: everything + extras + opus + MinerU (gated)
    .\install.ps1 -Preset senior           # bundle: default + Stop sound + thinking + maxed env
    .\install.ps1 -Preset minimum          # bundle: tools + safety only, no global git/hook mutation
    .\install.ps1 -WithMineru              # pre-warm doc2kb MinerU tier (gated, ~3 GB)
    .\install.ps1 -WithEnvDefaults         # merge maxed perf/privacy env into settings.json
    .\install.ps1 -WithCcstatusline        # add ccstatusline statusLine (install-if-missing)
    .\install.ps1 -WithCaveman             # install caveman (third-party curl|bash, gated)
    .\install.ps1 -ModelProfile opus       # all agents on opus (default: mixed)
    .\install.ps1 -ShowVersion             # show version

    Presets (ladder: minimum < default < senior < god; plus codex-full). A preset
    sets per-layer DEFAULTS; explicit flags override; -SkillsOnly wins. Resolved
    manifest prints before install and under -Dry.
      minimum     tools + safety only (no attribution-fix / claude-md / gost-validation)
      default     the no-flag baseline
      senior      default + Stop sound + thinking + maxed env defaults (xhigh)
      god         senior + ccstatusline + caveman + gh + claude-skip alias +
                  playwright-cli + opus + MinerU (caveman/gh/playwright/MinerU/alias
                  each gated by an interactive y/N)
      codex-full  Codex-native bundle (implies -Target codex): skills + gost-config +
                  Stop sound + launchers
#>
param(
    [ValidateSet("claude", "codex")]
    [string]$Target = "claude",
    # Preset bundle — validated manually below (empty default = no preset).
    [string]$Preset = "",
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
    [switch]$NoLaunchers,
    [switch]$WithSoundHooks,
    [switch]$WithNotificationSound,
    [switch]$WithThinkingSummaries,
    [switch]$WithMineru,
    [switch]$NoMineru,
    [switch]$WithEnvDefaults,
    [switch]$NoEnvDefaults,
    [switch]$WithCcstatusline,
    [switch]$NoCcstatusline,
    [switch]$WithCaveman,
    [switch]$NoCaveman,
    [switch]$WithGh,
    [switch]$NoGh,
    [switch]$WithClaudeSkip,
    [switch]$NoClaudeSkip,
    [switch]$WithPlaywright,
    [switch]$NoPlaywright,
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
$GostConfigSrc = Join-Path $ScriptDir "skills/gost-report/scripts/config.env.example"
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

# --- Preset resolution (before target/destination resolution) ---
# A preset fills per-layer DEFAULTS for layers the user did NOT pass explicitly
# ($PSBoundParameters tracks explicit flags). Explicit flags win; -SkillsOnly
# (applied after the target switch) wins over everything. codex-full flips the
# target to codex unless -Target was given. Mirror of install.sh apply_preset.
$Bound = $PSBoundParameters

if ($Preset -and $Preset -notin @("minimum", "default", "senior", "god", "codex-full")) {
    Write-Host "  Unknown -Preset: $Preset (use: minimum, default, senior, god, codex-full)" -ForegroundColor Red
    exit 1
}

switch ($Preset) {
    "minimum" {
        # Tools + safety only. config-defaults + gost-config stay on; strip the
        # layers that mutate global git config or install session hooks.
        if (-not $Bound.ContainsKey("NoAttributionFix")) { $NoAttributionFix = $true }
        if (-not $Bound.ContainsKey("NoClaudeMd"))        { $NoClaudeMd = $true }
        if (-not $Bound.ContainsKey("NoGostValidation"))  { $NoGostValidation = $true }
    }
    "default" { }
    "senior" {
        if (-not $Bound.ContainsKey("WithSoundHooks"))        { $WithSoundHooks = $true }
        if (-not $Bound.ContainsKey("WithThinkingSummaries")) { $WithThinkingSummaries = $true }
        if (-not ($Bound.ContainsKey("WithEnvDefaults") -or $Bound.ContainsKey("NoEnvDefaults"))) { $WithEnvDefaults = $true }
    }
    "god" {
        if (-not $Bound.ContainsKey("WithSoundHooks"))        { $WithSoundHooks = $true }
        if (-not $Bound.ContainsKey("WithThinkingSummaries")) { $WithThinkingSummaries = $true }
        if (-not ($Bound.ContainsKey("WithEnvDefaults") -or $Bound.ContainsKey("NoEnvDefaults")))   { $WithEnvDefaults = $true }
        if (-not ($Bound.ContainsKey("WithCcstatusline") -or $Bound.ContainsKey("NoCcstatusline"))) { $WithCcstatusline = $true }
        if (-not ($Bound.ContainsKey("WithCaveman") -or $Bound.ContainsKey("NoCaveman")))           { $WithCaveman = $true }
        if (-not ($Bound.ContainsKey("WithMineru") -or $Bound.ContainsKey("NoMineru")))             { $WithMineru = $true }
        if (-not ($Bound.ContainsKey("WithGh") -or $Bound.ContainsKey("NoGh")))                     { $WithGh = $true }
        if (-not ($Bound.ContainsKey("WithClaudeSkip") -or $Bound.ContainsKey("NoClaudeSkip")))     { $WithClaudeSkip = $true }
        if (-not ($Bound.ContainsKey("WithPlaywright") -or $Bound.ContainsKey("NoPlaywright")))     { $WithPlaywright = $true }
        # Notification sound left off (duplicates Stop). Model → opus unless set.
        if (-not $ModelProfile) { $ModelProfile = "opus" }
    }
    "codex-full" {
        if (-not $Bound.ContainsKey("Target"))         { $Target = "codex" }
        if (-not $Bound.ContainsKey("WithSoundHooks")) { $WithSoundHooks = $true }
        # launchers are on by default already
    }
}

# Resolve destinations from target. Codex skills go to ~/.codex/skills/.
$LegacyCodexSkillsDst = $null
switch ($Target) {
    "claude" {
        $Base = Join-Path $env:USERPROFILE ".claude"
        $AgentsDst   = Join-Path $Base "agents"
        $CommandsDst = Join-Path $Base "commands"
        $SkillsDst   = Join-Path $Base "skills"
    }
    "codex" {
        $Base = Join-Path $env:USERPROFILE ".codex"
        $LegacyCodexBase = Join-Path $env:USERPROFILE ".agents"
        $AgentsDst   = $null
        $CommandsDst = $null
        $SkillsDst   = Join-Path $Base "skills"
        $LegacyCodexSkillsDst = Join-Path $LegacyCodexBase "skills"
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
    $WithEnvDefaults = $false
    $WithCcstatusline = $false
    $WithCaveman = $false
    $WithGh = $false
    $WithClaudeSkip = $false
    $WithPlaywright = $false
}

function Write-Ok($msg)   { Write-Host "  $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  $msg" -ForegroundColor Yellow }
function Write-Info($msg) { Write-Host "  $msg" -ForegroundColor Cyan }
function Write-Err($msg)  { Write-Host "  $msg" -ForegroundColor Red }

function Show-CodexSkipNotice {
    if ($Target -eq "codex") {
        Write-Warn "Codex CLI has no custom slash commands - skipped commands/"
        Write-Warn "Codex agents use a different TOML format - skipped agents/. See README for details."
        Write-Info "Codex skills installed to ~/.codex/skills/."
    }
}

function Show-SkillsOnlyNotice {
    if ($SkillsOnly -and $Target -eq "claude") {
        Write-Warn "-SkillsOnly - skipped agents/, commands/, and all settings/hook layers"
    }
}

function Test-LegacyCodexCleanupActive {
    return ($Target -eq "codex" -and $LegacyCodexSkillsDst -and (Test-Path $LegacyCodexSkillsDst) -and (Test-Path $SkillsSrc))
}

function Remove-EmptyLegacyCodexDirs {
    if (-not $LegacyCodexSkillsDst) { return }

    if ((Test-Path $LegacyCodexSkillsDst) -and (@(Get-ChildItem $LegacyCodexSkillsDst -Force).Count -eq 0)) {
        Remove-Item $LegacyCodexSkillsDst -Force
        Write-Ok "removed legacy .agents/skills/"
    }

    $legacyAgentsDir = Split-Path $LegacyCodexSkillsDst -Parent
    if ((Test-Path $legacyAgentsDir) -and (@(Get-ChildItem $legacyAgentsDir -Force).Count -eq 0)) {
        Remove-Item $legacyAgentsDir -Force
        Write-Ok "removed legacy .agents/"
    }
}

function Remove-LegacyCodexSkills {
    $Script:LegacyCodexCleanedCount = 0
    if (-not (Test-LegacyCodexCleanupActive)) { return }

    Get-ChildItem $SkillsSrc -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        $legacyDst = Join-Path $LegacyCodexSkillsDst $_.Name
        if (Test-Path $legacyDst) {
            Remove-Item $legacyDst -Recurse -Force
            Write-Ok "removed legacy .agents/skills/$($_.Name)/"
            $Script:LegacyCodexCleanedCount++
        }
    }

    Remove-EmptyLegacyCodexDirs
}

function Show-LegacyCodexCleanupDry {
    if (-not (Test-LegacyCodexCleanupActive)) { return }

    $shown = $false
    Get-ChildItem $SkillsSrc -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        $legacyDst = Join-Path $LegacyCodexSkillsDst $_.Name
        if (Test-Path $legacyDst) {
            if (-not $shown) {
                Write-Host "Legacy Codex cleanup ($LegacyCodexSkillsDst):"
                $shown = $true
            }
            Write-Warn "- $($_.Name)/ (remove old .agents copy)"
        }
    }

    if ($shown) { Write-Host "" }
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
    return (($Target -eq "claude" -or $Target -eq "codex") -and $WithSoundHooks)
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

function Test-EnvDefaultsActive {
    return ($Target -eq "claude" -and $WithEnvDefaults -and -not $NoEnvDefaults)
}

function Test-CcstatuslineActive {
    return ($Target -eq "claude" -and $WithCcstatusline -and -not $NoCcstatusline)
}

function Test-CavemanActive {
    return ($Target -eq "claude" -and $WithCaveman -and -not $NoCaveman)
}

function Test-MineruPrewarmActive {
    # No target gate: doc2kb ships to both claude and codex.
    return ($WithMineru -and -not $NoMineru -and $SkillsDst)
}

function Test-GhInstallActive {
    # No target gate: gh helps both claude and codex users.
    return ($WithGh -and -not $NoGh)
}

function Test-ClaudeSkipActive {
    return ($Target -eq "claude" -and $WithClaudeSkip -and -not $NoClaudeSkip)
}

function Test-PlaywrightActive {
    # No target gate: the @playwright/cli skill serves Claude, Codex, Copilot, ...
    return ($WithPlaywright -and -not $NoPlaywright)
}

function Files-Equal($a, $b) {
    if (-not (Test-Path $a) -or -not (Test-Path $b)) { return $false }
    return (Get-FileHash $a).Hash -eq (Get-FileHash $b).Hash
}

function Read-SettingsJson {
    # Returns @{Hash=<hashtable>; Ok=$true} or @{Ok=$false} on parse error.
    $settings = Join-Path $script:Base "settings.json"
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

function Write-SettingsJson($settingsData) {
    $settings = Join-Path $script:Base "settings.json"
    New-Item -ItemType Directory -Path $script:Base -Force | Out-Null
    $tmp = "$settings.agentpipe.tmp"
    try {
        ($settingsData | ConvertTo-Json -Depth 32) | Set-Content -Path $tmp -Encoding UTF8 -NoNewline
        Add-Content -Path $tmp -Value "" -Encoding UTF8
        Move-Item -Path $tmp -Destination $settings -Force
        return $true
    } catch {
        if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
        Write-Warn "settings.json write failed: $_"
        return $false
    }
}

function Get-SoundHookConfigPath {
    if ($Target -eq "codex") {
        return (Join-Path $script:Base "hooks.json")
    }
    return (Join-Path $script:Base "settings.json")
}

function Read-SoundHookJson {
    # Returns @{Hash=<hashtable>; Ok=$true} or @{Ok=$false} on parse error.
    $path = Get-SoundHookConfigPath
    $base = @{}
    if (Test-Path $path) {
        try {
            $raw = (Get-Content $path -Raw -ErrorAction Stop)
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

function Write-SoundHookJson($hookData) {
    $path = Get-SoundHookConfigPath
    $dir = Split-Path $path -Parent
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    $tmp = "$path.agentpipe.tmp"
    try {
        ($hookData | ConvertTo-Json -Depth 32) | Set-Content -Path $tmp -Encoding UTF8 -NoNewline
        Add-Content -Path $tmp -Value "" -Encoding UTF8
        Move-Item -Path $tmp -Destination $path -Force
        return $true
    } catch {
        if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
        Write-Warn "$(Split-Path $path -Leaf) write failed: $_"
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

# --- gost-report persona config (XDG, install-if-missing) ---
# Skill reads the config at runtime to fill TitleConfig defaults: FIO, group,
# teacher. Path independent of skill install location, works for claude+codex.

function Get-GostConfigPath {
    if ($env:GOST_REPORT_CONFIG) { return $env:GOST_REPORT_CONFIG }
    if ($env:XDG_CONFIG_HOME)    { return (Join-Path $env:XDG_CONFIG_HOME "gost-report/config") }
    return (Join-Path $env:USERPROFILE ".config/gost-report/config")
}

function Test-GostConfigActive {
    return ($Script:SkillsDst -and (Test-Path $GostConfigSrc))
}

function Do-GostConfig {
    if (-not (Test-GostConfigActive)) { return }
    $dst = Get-GostConfigPath
    if (Test-Path $dst) {
        Write-Ok "gost-report/config already exists -> $dst (not overwriting)"
    } else {
        New-Item -ItemType Directory -Path (Split-Path $dst -Parent) -Force | Out-Null
        Copy-Item -Path $GostConfigSrc -Destination $dst -Force
        Write-Ok "gost-report/config installed (template) -> $dst"
        Write-Info "edit $dst to set FIO/group/teacher once, then omit them from build.py"
    }
}

function Do-GostConfigDry {
    if (-not (Test-GostConfigActive)) { return }
    Write-Host "gost-report persona config:"
    $dst = Get-GostConfigPath
    if (Test-Path $dst) {
        Write-Host "  = $dst (already exists, will not overwrite)"
    } else {
        Write-Info "+ $dst (template, install-if-missing)"
    }
    Write-Host ""
}

# --- Sound hooks (opt-in) ---
# -WithSoundHooks         → Stop sound hook only (one beep when Claude/Codex finishes)
# -WithNotificationSound  → Notification sound hook only (Claude permission/wait-for-input)
# Passing both is allowed; a warning is printed because Notification often
# fires right after Stop, producing two beeps in sequence.

# Sound-cue command pattern. Mirrors scripts/clean-sound-hooks.py — keep in sync.
$Script:SoundCmdPattern = '(?i)(\bafplay\b|\bpaplay\b|\[console\]::beep|powershell(\.exe)?\b[^|;&]*\bbeep\b)'

function Test-SoundCommand($cmd) {
    if (-not $cmd) { return $false }
    return [bool]([regex]::Match($cmd, $Script:SoundCmdPattern).Success)
}

function Merge-SoundHook($eventName, $cmd) {
    $r = Read-SoundHookJson
    $path = Get-SoundHookConfigPath
    if (-not $r.Ok) {
        Write-Warn "$(Split-Path $path -Leaf) has invalid JSON - skipping sound hook ($eventName)"
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

    if (Write-SoundHookJson $base) {
        $label = if ($Target -eq "codex") { "hooks.$eventName" } else { "settings/hooks.$eventName" }
        Write-Ok "$label (Windows beep) merged"
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
    if ($Target -eq "codex") {
        Write-Info "+ hooks.json/hooks.Stop (Windows beep)"
    } else {
        Write-Info "+ settings/hooks.Stop (Windows beep)"
    }
    Write-Host ""
}

function Do-NotificationSoundHookDry {
    if (-not (Test-NotificationSoundActive)) { return }
    Write-Host "Sound hook (Notification):"
    Write-Info "+ settings/hooks.Notification (Windows beep)"
    Write-Host ""
}

# -CleanSoundHooks action — strip every sound-hook entry (Stop+Notification)
# from hook config. Recognises afplay, paplay, [console]::beep, powershell beep.
# Leaves non-sound hooks (gost-validation, user customs) intact.
function Do-CleanSoundHooks {
    $path = Get-SoundHookConfigPath
    if (-not (Test-Path $path)) {
        Write-Info "no $(Split-Path $path -Leaf) at $path - nothing to clean"
        return
    }
    $r = Read-SoundHookJson
    if (-not $r.Ok) {
        Write-Err "$(Split-Path $path -Leaf) has invalid JSON - cannot clean sound hooks"
        exit 1
    }
    $base = $r.Hash
    if (-not $base.ContainsKey("hooks")) {
        Write-Info "no hooks section in $path - nothing to clean"
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
        if (Write-SoundHookJson $base) {
            $word = if ($totalRemoved -eq 1) { "entry" } else { "entries" }
            Write-Ok "removed $totalRemoved sound hook $word from $path"
        }
    } else {
        Write-Info "no sound hook entries found in $path"
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
# Codex target skips this validation layer.

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

# --- Env-defaults layer (claude target only, opt-in; senior/god) ---
# Merges a maxed perf/privacy block into settings.json "env". No secrets shipped.
# xhigh raises per-turn reasoning AND quota. Mirror of install.sh do_env_defaults.

function Do-EnvDefaults {
    if (-not (Test-EnvDefaultsActive)) { return }
    $r = Read-SettingsJson
    if (-not $r.Ok) { Write-Warn "settings.json has invalid JSON - skipping env-defaults"; return }
    $base = $r.Hash
    $envMap = @{}
    if ($base.ContainsKey("env") -and $base["env"]) {
        $base["env"].PSObject.Properties | ForEach-Object { $envMap[$_.Name] = $_.Value }
    }
    $envMap["CLAUDE_CODE_EFFORT_LEVEL"] = "xhigh"
    $envMap["CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"] = "1"
    $envMap["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    $base["env"] = $envMap
    if (Write-SettingsJson $base) {
        Write-Ok "settings/env merged (CLAUDE_CODE_EFFORT_LEVEL=xhigh + disable adaptive thinking + non-essential traffic)"
    }
}

function Do-EnvDefaultsDry {
    if (-not (Test-EnvDefaultsActive)) { return }
    Write-Host "Env defaults:"
    Write-Info "+ settings/env += CLAUDE_CODE_EFFORT_LEVEL=xhigh, DISABLE_ADAPTIVE_THINKING=1, DISABLE_NONESSENTIAL_TRAFFIC=1"
    Write-Host ""
}

# --- ccstatusline layer (claude target only, opt-in; god) ---
# Adds a statusLine block (runs ccstatusline via npx at render time; needs
# node/npx). Install-if-missing — never clobbers an existing statusLine.

function Test-HasStatusLine {
    $r = Read-SettingsJson
    if (-not $r.Ok) { return $false }
    return ($r.Hash.ContainsKey("statusLine") -and $r.Hash["statusLine"])
}

function Do-Ccstatusline {
    if (-not (Test-CcstatuslineActive)) { return }
    if (Test-HasStatusLine) { Write-Ok "ccstatusline - statusLine already set, leaving as-is"; return }
    $r = Read-SettingsJson
    if (-not $r.Ok) { Write-Warn "settings.json has invalid JSON - skipping ccstatusline"; return }
    $base = $r.Hash
    $base["statusLine"] = @{ type = "command"; command = "npx -y ccstatusline@2.2.19"; padding = 0; refreshInterval = 10 }
    if (Write-SettingsJson $base) {
        Write-Ok "settings/statusLine = ccstatusline (npx -y ccstatusline@2.2.19)"
        if (-not (Get-Command npx -ErrorAction SilentlyContinue)) {
            Write-Warn "  note: 'npx' not on PATH - install Node.js for the statusline to render"
        }
    }
}

function Do-CcstatuslineDry {
    if (-not (Test-CcstatuslineActive)) { return }
    Write-Host "ccstatusline:"
    if (Test-HasStatusLine) {
        Write-Host "  = statusLine already set (will not overwrite)"
    } else {
        Write-Info "+ settings/statusLine = ccstatusline (npx -y ccstatusline@2.2.19)"
    }
    Write-Host ""
}

# --- MinerU pre-warm (opt-in, gated; both targets ship doc2kb) ---
# HARD RULE: heavy ML deps never auto-installed. Requires explicit request AND an
# interactive y/N (default N). Non-interactive shells skip. Mirror of install.sh.

function Do-MineruPrewarm {
    if (-not (Test-MineruPrewarmActive)) { return }
    $ensure = Join-Path $SkillsDst "doc2kb\scripts\ensure_env.py"
    if (-not (Test-Path $ensure)) { Write-Warn "MinerU pre-warm skipped - doc2kb skill not installed this run"; return }
    $py = Get-Command python3 -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
    if (-not $py) { Write-Warn "MinerU pre-warm skipped - python not found"; return }
    if ([Console]::IsInputRedirected) {
        Write-Warn "MinerU pre-warm skipped (non-interactive shell)."
        Write-Info "run later: python `"$ensure`" --tier mineru"
        return
    }
    Write-Host ""
    Write-Warn "MinerU tier is a HEAVY download: ~3 GB+, several minutes (MLX/CUDA wheels)."
    $reply = Read-Host "  Pre-warm doc2kb MinerU tier now? [y/N]"
    if ($reply -match '^(y|yes)$') {
        Write-Info "Installing MinerU tier - this will take a while..."
        & $py.Source $ensure "--tier" "mineru"
        if ($LASTEXITCODE -eq 0) { Write-Ok "MinerU tier installed" }
        else { Write-Warn "MinerU tier install failed - run later: python `"$ensure`" --tier mineru" }
    } else {
        Write-Info "MinerU pre-warm declined - lightweight tier remains the default."
    }
}

function Do-MineruPrewarmDry {
    if (-not (Test-MineruPrewarmActive)) { return }
    Write-Host "MinerU pre-warm:"
    Write-Info "+ would pre-warm doc2kb MinerU tier (~3 GB, several minutes) - interactive y/N confirm at real install"
    Write-Host ""
}

# --- caveman (third-party, opt-in, gated; god) ---
# Pipes a remote install script to bash. Same hard gate as MinerU. Needs bash
# (Git Bash / WSL) on Windows. Mirror of install.sh do_caveman.

# Pinned to a reviewed commit (never moving `main`) + checksum-verified before exec.
# Bump CavemanRef + CavemanSha256 together on a deliberate upgrade. Mirror of install.sh.
$Script:CavemanRef = '655b7d9c5431f822264b7732e9901c5578ac84cf'
$Script:CavemanInstallUrl = "https://raw.githubusercontent.com/JuliusBrussee/caveman/$($Script:CavemanRef)/install.sh"
$Script:CavemanSha256 = '8ddef49c15f089c26affed3c31d97142c683e1d37a1499ae557281ca09c2712c'

function Do-Caveman {
    if (-not (Test-CavemanActive)) { return }
    # Pre-flight: caveman's installer is a `curl | bash` pipe, so bash must exist
    # (Git Bash / WSL on Windows). Check BEFORE prompting — never make the user
    # confirm an RCE that then cannot run. Mirrors install.sh's pre-prompt curl check.
    $bash = Get-Command bash -ErrorAction SilentlyContinue
    if (-not $bash) {
        Write-Warn "caveman skipped - bash not found (needs Git Bash / WSL)."
        Write-Info "run later (Git Bash/WSL): curl -fsSL $($Script:CavemanInstallUrl) | bash"
        return
    }
    if ([Console]::IsInputRedirected) {
        Write-Warn "caveman install skipped (non-interactive shell)."
        Write-Info "run later (Git Bash/WSL): curl -fsSL $($Script:CavemanInstallUrl) | bash"
        return
    }
    Write-Host ""
    Write-Warn "caveman is THIRD-PARTY code (github.com/JuliusBrussee/caveman). agentpipe runs"
    Write-Warn "  a pinned, checksum-verified commit - not whatever 'main' is today - but it is"
    Write-Warn "  still code agentpipe does not maintain. Needs node>=18. Pinned ref:"
    Write-Warn "  $($Script:CavemanRef)"
    $reply = Read-Host "  Install caveman now? [y/N]"
    if ($reply -match '^(y|yes)$') {
        Write-Info "Installing caveman (pinned $($Script:CavemanRef), checksum-verified)..."
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("caveman-" + [System.IO.Path]::GetRandomFileName() + ".sh")
        try {
            Invoke-WebRequest -Uri $Script:CavemanInstallUrl -OutFile $tmp -UseBasicParsing
            $got = (Get-FileHash -Algorithm SHA256 -Path $tmp).Hash.ToLower()
            if ($got -ne $Script:CavemanSha256) {
                Write-Err "caveman checksum MISMATCH - refusing to run (upstream changed or tampered)"
                Write-Warn "  expected $($Script:CavemanSha256)"
                Write-Warn "  got      $got"
            } else {
                & $bash.Source $tmp
                if ($LASTEXITCODE -eq 0) { Write-Ok "caveman installed" } else { Write-Warn "caveman install failed" }
            }
        } catch {
            Write-Warn "caveman download/verify failed: $_"
        } finally {
            if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
        }
    } else {
        Write-Info "caveman declined (pinned ref $($Script:CavemanRef))."
    }
}

function Do-CavemanDry {
    if (-not (Test-CavemanActive)) { return }
    Write-Host "caveman (third-party):"
    Write-Info "+ would offer caveman via curl|bash ($($Script:CavemanInstallUrl)) - interactive y/N at real install"
    Write-Host ""
}

# --- GitHub CLI (opt-in, gated; god) ---
# Installs gh via the system package manager IF missing (never intrudes when
# present). Gated by interactive y/N. Mirror of install.sh do_gh.

function Get-GhInstallCmd {
    if (Get-Command winget -ErrorAction SilentlyContinue) { return "winget install --id GitHub.cli -e --source winget" }
    if (Get-Command choco  -ErrorAction SilentlyContinue) { return "choco install gh -y" }
    if (Get-Command scoop  -ErrorAction SilentlyContinue) { return "scoop install gh" }
    return ""
}

function Do-Gh {
    if (-not (Test-GhInstallActive)) { return }
    if (Get-Command gh -ErrorAction SilentlyContinue) {
        Write-Ok "gh already installed - not intruding"
        return
    }
    $cmd = Get-GhInstallCmd
    if (-not $cmd) {
        Write-Warn "gh not installed and no known package manager (winget/choco/scoop) found."
        Write-Info "install manually: https://github.com/cli/cli#installation"
        return
    }
    if ([Console]::IsInputRedirected) {
        Write-Warn "gh install skipped (non-interactive shell)."
        Write-Info "run later: $cmd"
        return
    }
    Write-Host ""
    Write-Warn "GitHub CLI (gh) is not installed. Install it now via:"
    Write-Warn "  $cmd"
    $reply = Read-Host "  Install gh? [y/N]"
    if ($reply -match '^(y|yes)$') {
        Write-Info "Installing gh..."
        Invoke-Expression $cmd
        if (Get-Command gh -ErrorAction SilentlyContinue) { Write-Ok "gh installed" } else { Write-Warn "gh install failed - run later: $cmd" }
    } else {
        Write-Info "gh install declined - run later: $cmd"
    }
}

function Do-GhDry {
    if (-not (Test-GhInstallActive)) { return }
    Write-Host "GitHub CLI (gh):"
    if (Get-Command gh -ErrorAction SilentlyContinue) {
        Write-Host "  = gh already installed (will not intrude)"
    } else {
        Write-Info "+ would offer to install gh via $(Get-GhInstallCmd) - interactive y/N at real install"
    }
    Write-Host ""
}

# --- claude-skip alias (claude target only, opt-in, gated; god) ---
# Adds a `claude-skip` function (claude --dangerously-skip-permissions) to the
# PowerShell $PROFILE. Deliberately not named `claude`. Gated + security warning,
# never duplicates. Mirror of install.sh do_claude_skip.

$Script:ClaudeSkipFnLine = 'function claude-skip { claude --dangerously-skip-permissions @args }'

function Do-ClaudeSkip {
    if (-not (Test-ClaudeSkipActive)) { return }
    $rc = $PROFILE
    if ((Test-Path $rc) -and (Select-String -Quiet -Path $rc -Pattern 'function claude-skip' -ErrorAction SilentlyContinue)) {
        Write-Ok "claude-skip function already in $rc - not duplicating"
        return
    }
    if ([Console]::IsInputRedirected) {
        Write-Warn "claude-skip alias skipped (non-interactive shell)."
        Write-Info "add manually to $rc :  $($Script:ClaudeSkipFnLine)"
        return
    }
    Write-Host ""
    Write-Warn "SECURITY: 'claude-skip' runs Claude Code with --dangerously-skip-permissions,"
    Write-Warn "  which bypasses ALL permission prompts - Claude can run any command without"
    Write-Warn "  asking. Prefer an OS sandbox / devcontainer over the host shell; use only in"
    Write-Warn "  trusted or throwaway dirs. Named 'claude-skip' (not 'claude') so plain"
    Write-Warn "  'claude' stays safe. Persists in `$PROFILE until 'install.ps1 -Uninstall'."
    $reply = Read-Host "  Add 'claude-skip' to $rc ? [y/N]"
    if ($reply -match '^(y|yes)$') {
        New-Item -ItemType Directory -Path (Split-Path $rc -Parent) -Force | Out-Null
        Add-Content -Path $rc -Value "`n# agentpipe: opt-in dangerous skip-permissions alias`n$($Script:ClaudeSkipFnLine)"
        Write-Ok "claude-skip added to $rc (reload: . `$PROFILE)"
    } else {
        Write-Info "claude-skip declined."
        Write-Info "add manually to $rc :  $($Script:ClaudeSkipFnLine)"
    }
}

function Do-ClaudeSkipDry {
    if (-not (Test-ClaudeSkipActive)) { return }
    Write-Host "claude-skip alias:"
    $rc = $PROFILE
    if ((Test-Path $rc) -and (Select-String -Quiet -Path $rc -Pattern 'function claude-skip' -ErrorAction SilentlyContinue)) {
        Write-Host "  = claude-skip already in $rc (will not duplicate)"
    } else {
        Write-Info "+ would offer to add 'claude-skip' to $rc - interactive y/N + security warning"
    }
    Write-Host ""
}

# Reversal: strip the agentpipe-added claude-skip function (+ marker comment) from
# $PROFILE on uninstall. Marker-scoped — never touches user-authored lines.
function Do-ClaudeSkipUnfix {
    if ($Target -ne "claude") { return }
    $rc = $PROFILE
    if (-not ($rc -and (Test-Path $rc))) { return }
    if (-not (Select-String -Quiet -Path $rc -Pattern 'function claude-skip' -ErrorAction SilentlyContinue)) { return }
    $kept = Get-Content -Path $rc | Where-Object {
        ($_ -notmatch 'function claude-skip') -and ($_ -notmatch 'agentpipe: opt-in dangerous skip-permissions alias')
    }
    Set-Content -Path $rc -Value $kept -Encoding UTF8
    Write-Ok "removed claude-skip from $rc"
}

# Reversal: drop the statusLine key on uninstall ONLY if it is still agentpipe's
# ccstatusline command (never clobber a user-customised statusLine).
function Do-CcstatuslineUnfix {
    if ($Target -ne "claude") { return }
    $r = Read-SettingsJson
    if (-not $r.Ok) { return }
    $base = $r.Hash
    if (-not $base.ContainsKey("statusLine")) { return }
    $sl = $base["statusLine"]
    $cmd = ""
    if ($sl -is [hashtable]) { $cmd = [string]$sl["command"] }
    elseif ($sl -and ($sl.PSObject.Properties.Name -contains "command")) { $cmd = [string]$sl.command }
    if ($cmd -match 'ccstatusline') {
        $base.Remove("statusLine")
        if (Write-SettingsJson $base) { Write-Ok "removed ccstatusline statusLine from settings.json" }
    }
}

# --- Playwright CLI (opt-in, gated; god) ---
# Installs Microsoft's @playwright/cli + its bundled skill (never vendored here).
# Skip if a playwright cli/mcp is already present. Mirror of install.sh do_playwright.

function Test-PlaywrightPresent {
    if (Get-Command playwright-cli -ErrorAction SilentlyContinue) { return $true }
    if (Get-Command npx -ErrorAction SilentlyContinue) {
        & npx --no-install playwright-cli --version *> $null
        if ($LASTEXITCODE -eq 0) { return $true }
    }
    $settings = Join-Path $script:Base "settings.json"
    if ((Test-Path $settings) -and (Select-String -Quiet -Path $settings -Pattern '"playwright"' -ErrorAction SilentlyContinue)) { return $true }
    return $false
}

function Do-Playwright {
    if (-not (Test-PlaywrightActive)) { return }
    if (Test-PlaywrightPresent) {
        Write-Ok "playwright (cli or mcp) already present - not intruding"
        return
    }
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Write-Warn "playwright-cli skipped - npm not found (install Node.js first)"
        Write-Info "then run: npm install -g @playwright/cli@0.1.13; playwright-cli install --skills"
        return
    }
    if ([Console]::IsInputRedirected) {
        Write-Warn "playwright-cli install skipped (non-interactive shell)."
        Write-Info "run later: npm install -g @playwright/cli@0.1.13; playwright-cli install --skills"
        return
    }
    Write-Host ""
    Write-Warn "Playwright CLI (@playwright/cli) gives terminal browser automation with"
    Write-Warn "  persistent + parallel sessions, and ships its own agent skill. Installs via:"
    Write-Warn "  npm install -g @playwright/cli@0.1.13 ; playwright-cli install --skills"
    $reply = Read-Host "  Install @playwright/cli + skill now? [y/N]"
    if ($reply -match '^(y|yes)$') {
        Write-Info "Installing @playwright/cli..."
        & npm install -g '@playwright/cli@0.1.13'
        if ($LASTEXITCODE -eq 0) {
            & playwright-cli install --skills
            if ($LASTEXITCODE -eq 0) { Write-Ok "playwright-cli + skill installed" } else { Write-Warn "playwright-cli skill step failed - run: playwright-cli install --skills" }
        } else {
            Write-Warn "playwright-cli install failed - run later: npm install -g @playwright/cli@0.1.13; playwright-cli install --skills"
        }
    } else {
        Write-Info "playwright-cli declined."
        Write-Info "install later: npm install -g @playwright/cli@0.1.13; playwright-cli install --skills"
    }
}

function Do-PlaywrightDry {
    if (-not (Test-PlaywrightActive)) { return }
    Write-Host "Playwright CLI:"
    if (Test-PlaywrightPresent) {
        Write-Host "  = playwright already present (cli or mcp) - will not intrude"
    } else {
        Write-Info "+ would offer: npm install -g @playwright/cli@0.1.13; playwright-cli install --skills - interactive y/N"
    }
    Write-Host ""
}

# --- Preset manifest + codex-downgrade notice ---

function Get-MState([scriptblock]$pred) { if (& $pred) { return "on" } else { return "off" } }

function Show-PresetManifest {
    if (-not $Preset) { return }
    Write-Host ""
    Write-Info "Preset '$Preset' resolved -> target=$Target, model-profile=$ModelProfile"
    if ($SkillsOnly) { Write-Host "    mode:                skills-only (agents/commands/settings layers off)" }
    Write-Host "    attribution-fix:     $(Get-MState { Test-AttributionActive })"
    Write-Host "    config-defaults:     $(Get-MState { Test-ConfigDefaultsActive })"
    Write-Host "    claude-md baseline:  $(Get-MState { Test-ClaudeMdActive })"
    Write-Host "    gost persona config: $(Get-MState { Test-GostConfigActive })"
    Write-Host "    gost-validation:     $(Get-MState { Test-GostValidationActive })"
    Write-Host "    launchers gr/us/dkb: $(Get-MState { Test-LaunchersActive })"
    Write-Host "    stop sound:          $(Get-MState { Test-StopSoundActive })"
    Write-Host "    notification sound:  $(Get-MState { Test-NotificationSoundActive })"
    Write-Host "    thinking summaries:  $(Get-MState { Test-ThinkingSummariesActive })"
    Write-Host "    env defaults (xhigh): $(Get-MState { Test-EnvDefaultsActive })"
    Write-Host "    ccstatusline:        $(Get-MState { Test-CcstatuslineActive })"
    Write-Host "    caveman (3rd-party): $(Get-MState { Test-CavemanActive })"
    Write-Host "    MinerU pre-warm:     $(Get-MState { Test-MineruPrewarmActive })"
    Write-Host "    gh CLI (if missing): $(Get-MState { Test-GhInstallActive })"
    Write-Host "    claude-skip alias:   $(Get-MState { Test-ClaudeSkipActive })"
    Write-Host "    playwright-cli:      $(Get-MState { Test-PlaywrightActive })"
}

function Show-PresetCodexDowngradeNotice {
    if ($Target -ne "codex") { return }
    if ($Preset -in @("minimum", "default", "senior", "god")) {
        Write-Warn "Preset '$Preset' targets Claude Code; under -Target codex only"
        Write-Warn "  skills/gost-config/stop-sound/launchers apply. Use -Preset codex-full."
    }
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

# --- CLI launchers (gr/us/dkb on PATH) — mirror of install.sh ---
# Short commands so agents and humans call `gr build.py` instead of the long
# ensure_env.py invocation. Each shim bakes the installed skill path (env-
# overridable) and execs the skill entry. Installed with the skills unless a
# same-named command already exists on PATH (then skipped, never clobbered).

function Test-LaunchersActive { return ((-not $NoLaunchers) -and $SkillsDst) }

function Get-LauncherBinDir {
    if ($env:AGENTPIPE_BIN_DIR) { return $env:AGENTPIPE_BIN_DIR }
    return (Join-Path $HOME ".local\bin")
}

function Get-GrShim {
    $skill = Join-Path $SkillsDst "gost-report"
    return @"
@echo off
REM agentpipe-launcher
set "SKILL=%GOST_REPORT_SKILL%"
if "%SKILL%"=="" set "SKILL=$skill"
python "%SKILL%\scripts\cli.py" %*
"@
}

function Get-UsShim {
    $skill = Join-Path $SkillsDst "ultrasearch"
    return @"
@echo off
REM agentpipe-launcher
set "SKILL=%ULTRASEARCH_SKILL%"
if "%SKILL%"=="" set "SKILL=$skill"
python "%SKILL%\scripts\ensure_env.py" ultrasearch.py %*
"@
}

function Get-DkbShim {
    $skill = Join-Path $SkillsDst "doc2kb"
    return @"
@echo off
REM agentpipe-launcher
set "SKILL=%DOC2KB_SKILL%"
if "%SKILL%"=="" set "SKILL=$skill"
python "%SKILL%\scripts\dkb.py" %*
"@
}

function Write-Launcher($cmd, $content) {
    $bindir = Get-LauncherBinDir
    $target = Join-Path $bindir "$cmd.cmd"
    $existing = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($existing -and $existing.Source -and ($existing.Source -ne $target)) {
        if (-not (Select-String -Quiet -Path $existing.Source -Pattern 'agentpipe-launcher' -ErrorAction SilentlyContinue)) {
            Write-Warn "launcher '$cmd' skipped - '$($existing.Source)' already on PATH (not agentpipe's)"
            return
        }
    }
    New-Item -ItemType Directory -Force -Path $bindir | Out-Null
    Set-Content -Path $target -Value $content -Encoding ASCII
    Write-Ok "launcher $cmd -> $target"
    $Script:LauncherInstalled = $true
}

function Show-LauncherPathNotice {
    $bindir = Get-LauncherBinDir
    $onPath = ($env:PATH -split ';') -contains $bindir
    if (-not $onPath) {
        Write-Warn "add $bindir to PATH to use gr/us/dkb"
    }
}

function Do-Launchers {
    if (-not (Test-LaunchersActive)) { return }
    Write-Host ""
    Write-Info "CLI launchers -> $(Get-LauncherBinDir)"
    if (Test-Path (Join-Path $SkillsDst "gost-report")) { Write-Launcher "gr"  (Get-GrShim) }
    if (Test-Path (Join-Path $SkillsDst "ultrasearch")) { Write-Launcher "us"  (Get-UsShim) }
    if (Test-Path (Join-Path $SkillsDst "doc2kb"))      { Write-Launcher "dkb" (Get-DkbShim) }
    if ($Script:LauncherInstalled) { Show-LauncherPathNotice }
}

function Do-LaunchersRemove {
    if (-not (Test-LaunchersActive)) { return }
    $bindir = Get-LauncherBinDir
    foreach ($cmd in @("gr", "us", "dkb")) {
        $target = Join-Path $bindir "$cmd.cmd"
        if ((Test-Path $target) -and (Select-String -Quiet -Path $target -Pattern 'agentpipe-launcher' -ErrorAction SilentlyContinue)) {
            Remove-Item $target -Force
            Write-Ok "removed launcher $cmd"
        }
    }
}

function Do-LaunchersDry {
    if (-not (Test-LaunchersActive)) { return }
    $bindir = Get-LauncherBinDir
    Write-Host "CLI launchers ($bindir):"
    foreach ($cmd in @("gr", "us", "dkb")) {
        $target = Join-Path $bindir "$cmd.cmd"
        $existing = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($existing -and $existing.Source) {
            if (($existing.Source -ne $target) -and -not (Select-String -Quiet -Path $existing.Source -Pattern 'agentpipe-launcher' -ErrorAction SilentlyContinue)) {
                Write-Warn "  ! $cmd (conflict: $($existing.Source) - would skip)"
            } else {
                Write-Host "  = $cmd (update)"
            }
        } else {
            Write-Info "  + $cmd (NEW)"
        }
    }
    Write-Host ""
}

function Do-Install {
    if ($AgentsDst) {
        Write-Info "Installing agentpipe v$($Script:Version) (target: $Target, model-profile: $ModelProfile) to: $Base"
    } else {
        Write-Info "Installing agentpipe v$($Script:Version) (target: $Target) to: $Base"
    }
    Show-PresetManifest
    Show-PresetCodexDowngradeNotice
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
        New-Item -ItemType Directory -Path $SkillsDst -Force | Out-Null
        Get-ChildItem $SkillsSrc -Directory | ForEach-Object {
            $dst = Join-Path $SkillsDst $_.Name
            # ADR-008: move any legacy in-skill durable state (e.g. ultrasearch
            # corpus.db) out to the global state dir BEFORE removing the old code.
            # The freshly-shipped ensure_env owns the move (no-op when no data);
            # runtime migration alone loses the race with the Remove-Item below.
            if (Test-Path $dst) {
                $py = Get-Command python3 -ErrorAction SilentlyContinue
                if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
                $envScript = Join-Path $_.FullName "scripts\ensure_env.py"
                if ($py -and (Test-Path $envScript)) {
                    try { & $py.Source $envScript "--migrate-from" $dst 2>&1 | Out-Null } catch { }
                }
                Remove-Item $dst -Recurse -Force
            }
            Copy-Item $_.FullName -Destination $dst -Recurse -Force
            # Strip dev cruft a source checkout carries — the venv and any runtime
            # data (a dev clone may hold a corpus/cache); these must never ship.
            # Mirrors the exclusions in scripts/build-skills.sh. Runtime state
            # lives in the global state dir now (ADR-008).
            foreach ($cruft in @(".venv", ".venv.lock",
                                 "data\corpus.db", "data\corpus.db-wal", "data\corpus.db-shm",
                                 "data\cache", "data\retraction_watch.csv",
                                 "data\retraction_watch.csv.tmp", "data\_logs")) {
                Remove-Item -Path (Join-Path $dst $cruft) -Recurse -Force -ErrorAction SilentlyContinue
            }
            Write-Ok "skills/$($_.Name)/"
            $count++
        }
    }

    Remove-LegacyCodexSkills

    if (Test-AttributionActive) {
        Write-Host ""
        Do-AttributionFix
    }

    if (Test-ConfigDefaultsActive) {
        Write-Host ""
        Do-ConfigDefaults
    }

    if (Test-EnvDefaultsActive) {
        Write-Host ""
        Do-EnvDefaults
    }

    if (Test-CcstatuslineActive) {
        Write-Host ""
        Do-Ccstatusline
    }

    if (Test-ClaudeMdActive) {
        Write-Host ""
        Do-ClaudeMd
    }

    if (Test-GostConfigActive) {
        Write-Host ""
        Do-GostConfig
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

    Do-Launchers

    Do-MineruPrewarm
    Do-Caveman
    Do-Gh
    Do-ClaudeSkip
    Do-Playwright

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
        Get-ChildItem $SkillsSrc -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $dst = Join-Path $SkillsDst $_.Name
            if (Test-Path $dst) {
                Remove-Item $dst -Recurse -Force
                Write-Ok "removed skills/$($_.Name)/"
                $count++
            }
        }
        # Skill runtime state (venvs, ultrasearch corpus) lives in a global dir
        # outside the code tree (ADR-008), shared across install targets, so
        # uninstall leaves it untouched. Point the user at it.
        $stateRoot = $env:AGENTPIPE_HOME
        if (-not $stateRoot) {
            $stateRoot = if ($env:XDG_DATA_HOME) { Join-Path $env:XDG_DATA_HOME "agentpipe" }
                         elseif ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "agentpipe" }
                         else { Join-Path (Join-Path $HOME "AppData\Local") "agentpipe" }
        }
        if (Test-Path $stateRoot) {
            Write-Info "skill state preserved (shared across targets): $stateRoot"
            Write-Info "  remove manually if no longer needed (deletes venvs + ultrasearch corpus)"
        }
    }

    Remove-LegacyCodexSkills
    $count += $Script:LegacyCodexCleanedCount

    foreach ($d in @($AgentsDst, $CommandsDst, $SkillsDst)) {
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

    # Reverse the marker-scoped god side-effects we CAN safely undo.
    if ($Target -eq "claude") {
        Do-ClaudeSkipUnfix
        Do-CcstatuslineUnfix
    }

    Do-LaunchersRemove

    # State agentpipe cannot safely auto-reverse (external tools live in the user's
    # package managers; settings.json env/hooks may be user-merged).
    Write-Host ""
    Write-Info "Left in place (remove manually if you no longer want them):"
    Write-Info "  settings.json env/hooks keys (env xhigh, sound, gost-validation) - edit settings.json"
    Write-Info "  caveman    - see github.com/JuliusBrussee/caveman for removal"
    Write-Info "  gh         - your package manager (winget uninstall / choco uninstall gh / ...)"
    Write-Info "  @playwright/cli - npm uninstall -g @playwright/cli"
    Write-Info "  MinerU tier (doc2kb) - delete the doc2kb venv under your agentpipe state dir"

    Write-Host ""
    Write-Info "Removed $count items from $Base"
}

function Do-Dry {
    Write-Info "Dry run (target: $Target) - would install to: $Base"
    Show-PresetManifest
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
        Write-Host "Skills ($SkillsDst):"
        Get-ChildItem $SkillsSrc -Directory | ForEach-Object {
            $dst = Join-Path $SkillsDst $_.Name
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

    Show-LegacyCodexCleanupDry

    Do-LaunchersDry
    Do-AttributionDry
    Do-ConfigDefaultsDry
    Do-EnvDefaultsDry
    Do-CcstatuslineDry
    Do-ClaudeMdDry
    Do-GostConfigDry
    Warn-SoundOverlap
    Do-StopSoundHookDry
    Do-NotificationSoundHookDry
    Do-ThinkingSummariesDry
    Do-GostValidationDry
    Do-MineruPrewarmDry
    Do-CavemanDry
    Do-GhDry
    Do-ClaudeSkipDry
    Do-PlaywrightDry
    Show-CodexSkipNotice
    Show-SkillsOnlyNotice
    Show-PresetCodexDowngradeNotice
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
        Get-ChildItem $SkillsSrc -Directory | ForEach-Object {
            $dst = Join-Path $SkillsDst $_.Name
            if (Test-Path $dst) {
                $srcFiles = Get-ChildItem $_.FullName -Recurse -File | Sort-Object FullName
                $dstFiles = Get-ChildItem $dst -Recurse -File | Sort-Object FullName
                $srcSig = ($srcFiles | ForEach-Object { (Get-FileHash $_.FullName).Hash }) -join ""
                $dstSig = ($dstFiles | ForEach-Object { (Get-FileHash $_.FullName).Hash }) -join ""
                if ($srcSig -ne $dstSig -or $srcFiles.Count -ne $dstFiles.Count) {
                    Write-Warn "skills/$($_.Name)/ differs"
                    $hasDiff = $true
                }
            } else {
                Write-Warn "skills/$($_.Name)/ - not installed"
                $hasDiff = $true
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

    if ($SkillsDst -and (Test-Path $SkillsDst) -and (Test-Path $SkillsSrc)) {
        Get-ChildItem $SkillsSrc -Directory | ForEach-Object {
            $dst = Join-Path $SkillsDst $_.Name
            if (Test-Path $dst) {
                $repoCopy = Join-Path $SkillsSrc $_.Name
                if (Test-Path $repoCopy) { Remove-Item $repoCopy -Recurse -Force }
                Copy-Item $dst -Destination $repoCopy -Recurse -Force
                Remove-Item -Path (Join-Path $repoCopy ".venv") -Recurse -Force -ErrorAction SilentlyContinue
                Remove-Item -Path (Join-Path $repoCopy ".venv.lock") -Force -ErrorAction SilentlyContinue
                Write-Ok "skills/$($_.Name)/ <- installed"
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
