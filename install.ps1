#Requires -Version 5.1
<#
.SYNOPSIS
    agentpipe — Install Script (Windows PowerShell)
.DESCRIPTION
    Copies agents, commands, and skills from this repo to ~/.claude/ (Claude Code, default)
    or ~/.agents/skills/ (Codex CLI, with -Target codex; agents and commands are skipped
    because Codex agents use a different TOML format and Codex CLI has no custom slash commands).
.EXAMPLE
    .\install.ps1                        # install for Claude Code (default)
    .\install.ps1 -Target codex          # install for Codex CLI (skills only)
    .\install.ps1 -Dry                   # preview what would be copied
    .\install.ps1 -Diff                  # show repo vs installed differences
    .\install.ps1 -Pull                  # copy installed back to repo
    .\install.ps1 -Update                # git pull --ff-only, then install
    .\install.ps1 -Uninstall             # remove installed files
    .\install.ps1 -NoAttributionFix      # skip Co-Authored-By suppression layer
    .\install.ps1 -NoConfigDefaults      # skip $schema + secret deny-list
    .\install.ps1 -ShowVersion           # show version
#>
param(
    [ValidateSet("claude", "codex")]
    [string]$Target = "claude",
    [switch]$Dry,
    [switch]$Diff,
    [switch]$Pull,
    [switch]$Update,
    [switch]$Uninstall,
    [switch]$NoAttributionFix,
    [switch]$NoConfigDefaults,
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
$GitTemplateDir = Join-Path $env:USERPROFILE ".git-templates"
$GitHookDst = Join-Path $GitTemplateDir "hooks/commit-msg"
$ConfigSchemaUrl = "https://json.schemastore.org/claude-code-settings.json"
$ConfigDenyList = @(
    "Read(./.env)"
    "Read(./.env.*)"
    "Read(./**/secrets/**)"
    "Read(./**/*.pem)"
    "Read(./**/*.key)"
)

# Resolve destinations from target. Codex skills go to ~/.agents/skills/ (open-agent-skills
# standard), NOT ~/.codex/skills/. ~/.codex/ holds config and TOML agents.
switch ($Target) {
    "claude" {
        $Base = Join-Path $env:USERPROFILE ".claude"
        $AgentsDst   = Join-Path $Base "agents"
        $CommandsDst = Join-Path $Base "commands"
        $SkillsDst   = Join-Path $Base "skills"
    }
    "codex" {
        $Base = Join-Path $env:USERPROFILE ".agents"
        $AgentsDst   = $null
        $CommandsDst = $null
        $SkillsDst   = Join-Path $Base "skills"
    }
}

function Write-Ok($msg)   { Write-Host "  $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  $msg" -ForegroundColor Yellow }
function Write-Info($msg) { Write-Host "  $msg" -ForegroundColor Cyan }
function Write-Err($msg)  { Write-Host "  $msg" -ForegroundColor Red }

function Show-CodexSkipNotice {
    if ($Target -eq "codex") {
        Write-Warn "Codex CLI has no custom slash commands - skipped commands/"
        Write-Warn "Codex agents use a different TOML format - skipped agents/. See README for details."
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

    # $schema (overwrite scalar)
    $base["`$schema"] = $ConfigSchemaUrl

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
        Write-Ok "settings/`$schema + permissions.deny (secrets) merged"
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
    if ((Test-Path $settings) -and (Select-String -Path $settings -SimpleMatch -Pattern $ConfigSchemaUrl -Quiet)) {
        Write-Host "  = settings/`$schema (already set)"
    } else {
        Write-Info "+ settings/`$schema=$ConfigSchemaUrl"
    }
    if ((Test-Path $settings) -and (Select-String -Path $settings -SimpleMatch -Pattern 'Read(./**/*.key)' -Quiet)) {
        Write-Host "  = settings/permissions.deny secrets (already set)"
    } else {
        Write-Info "+ settings/permissions.deny += [.env, *.pem, *.key, secrets/**]"
    }
    Write-Host ""
}

function Do-Install {
    Write-Info "Installing agentpipe v$($Script:Version) (target: $Target) to: $Base"
    $count = 0

    if ($AgentsDst) {
        New-Item -ItemType Directory -Path $AgentsDst -Force | Out-Null
        Get-ChildItem "$AgentsSrc\*.md" -ErrorAction SilentlyContinue | ForEach-Object {
            Copy-Item $_.FullName -Destination (Join-Path $AgentsDst $_.Name) -Force
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

    if ($SkillsDst -and (Test-Path $SkillsSrc)) {
        New-Item -ItemType Directory -Path $SkillsDst -Force | Out-Null
        Get-ChildItem $SkillsSrc -Directory | ForEach-Object {
            $dst = Join-Path $SkillsDst $_.Name
            if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
            Copy-Item $_.FullName -Destination $dst -Recurse -Force
            # Скиллы могут держать свой venv в .venv/ (создаётся
            # bootstrap-скриптом). Не тащим dev venv в системную установку.
            Remove-Item -Path (Join-Path $dst ".venv") -Recurse -Force -ErrorAction SilentlyContinue
            Remove-Item -Path (Join-Path $dst ".venv.lock") -Force -ErrorAction SilentlyContinue
            Write-Ok "skills/$($_.Name)/"
            $count++
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

    Write-Host ""
    Write-Info "Installed $count items to $Base"
    Show-CodexSkipNotice
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

    if ($SkillsDst -and (Test-Path $SkillsSrc)) {
        Get-ChildItem $SkillsSrc -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $dst = Join-Path $SkillsDst $_.Name
            if (Test-Path $dst) {
                Remove-Item $dst -Recurse -Force
                Write-Ok "removed skills/$($_.Name)/"
                $count++
            }
        }
    }

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

    Write-Host ""
    Write-Info "Removed $count items from $Base"
}

function Do-Dry {
    Write-Info "Dry run (target: $Target) - would install to: $Base"
    Write-Host ""

    if ($AgentsDst) {
        Write-Host "Agents:"
        Get-ChildItem "$AgentsSrc\*.md" -ErrorAction SilentlyContinue | ForEach-Object {
            $dst = Join-Path $AgentsDst $_.Name
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

    if ($SkillsDst -and (Test-Path $SkillsSrc)) {
        Write-Host "Skills:"
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

    Do-AttributionDry
    Do-ConfigDefaultsDry
    Show-CodexSkipNotice
}

function Do-Diff {
    Write-Info "Comparing repo <-> installed at $Base (target: $Target)"
    $hasDiff = $false

    $pairs = @()
    if ($AgentsDst)   { $pairs += @{ Label = "agents"; Src = $AgentsSrc; Dst = $AgentsDst } }
    if ($CommandsDst) { $pairs += @{ Label = "commands"; Src = $CommandsSrc; Dst = $CommandsDst } }

    foreach ($pair in $pairs) {
        Get-ChildItem "$($pair.Src)\*.md" -ErrorAction SilentlyContinue | ForEach-Object {
            $dstFile = Join-Path $pair.Dst $_.Name
            if (Test-Path $dstFile) {
                $srcContent = Get-Content $_.FullName -Raw
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

    if ($SkillsDst -and (Test-Path $SkillsSrc)) {
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
        Get-ChildItem "$AgentsDst\*.md" -ErrorAction SilentlyContinue | ForEach-Object {
            Copy-Item $_.FullName -Destination (Join-Path $AgentsSrc $_.Name) -Force
            Write-Ok "agents/$($_.Name) <- installed"
            $count++
        }
    }

    if ($CommandsDst -and (Test-Path $CommandsDst)) {
        Get-ChildItem "$CommandsDst\*.md" -ErrorAction SilentlyContinue | ForEach-Object {
            Copy-Item $_.FullName -Destination (Join-Path $CommandsSrc $_.Name) -Force
            Write-Ok "commands/$($_.Name) <- installed"
            $count++
        }
    }

    if ($SkillsDst -and (Test-Path $SkillsSrc)) {
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
else { Do-Install }
