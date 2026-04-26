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
    .\install.ps1 -Uninstall             # remove installed files
    .\install.ps1 -ShowVersion           # show version
#>
param(
    [ValidateSet("claude", "codex")]
    [string]$Target = "claude",
    [switch]$Dry,
    [switch]$Diff,
    [switch]$Pull,
    [switch]$Uninstall,
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
            Write-Ok "skills/$($_.Name)/"
            $count++
        }
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
    }

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

    if (-not $hasDiff) {
        Write-Ok "Everything in sync"
    }
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
elseif ($Uninstall) { Do-Uninstall }
else { Do-Install }
