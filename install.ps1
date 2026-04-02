#Requires -Version 5.1
<#
.SYNOPSIS
    Claude Code Agent Team — Install Script (Windows PowerShell)
.DESCRIPTION
    Copies agents and commands from this repo to ~/.claude/
.EXAMPLE
    .\install.ps1              # install
    .\install.ps1 -Dry         # show what would be copied
    .\install.ps1 -Diff        # show differences
    .\install.ps1 -Pull        # pull installed versions back to repo
#>
param(
    [switch]$Dry,
    [switch]$Diff,
    [switch]$Pull,
    [switch]$Help
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AgentsSrc = Join-Path $ScriptDir "agents"
$CommandsSrc = Join-Path $ScriptDir "commands"

$ClaudeHome = Join-Path $env:USERPROFILE ".claude"
$AgentsDst = Join-Path $ClaudeHome "agents"
$CommandsDst = Join-Path $ClaudeHome "commands"

function Write-Ok($msg) { Write-Host "  $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  $msg" -ForegroundColor Yellow }
function Write-Info($msg) { Write-Host "  $msg" -ForegroundColor Cyan }
function Write-Err($msg) { Write-Host "  $msg" -ForegroundColor Red }

function Do-Install {
    Write-Info "Installing to: $ClaudeHome"

    New-Item -ItemType Directory -Path $AgentsDst -Force | Out-Null
    New-Item -ItemType Directory -Path $CommandsDst -Force | Out-Null

    $count = 0

    Get-ChildItem "$AgentsSrc\*.md" | ForEach-Object {
        Copy-Item $_.FullName -Destination (Join-Path $AgentsDst $_.Name) -Force
        Write-Ok "agents/$($_.Name)"
        $count++
    }

    Get-ChildItem "$CommandsSrc\*.md" | ForEach-Object {
        Copy-Item $_.FullName -Destination (Join-Path $CommandsDst $_.Name) -Force
        Write-Ok "commands/$($_.Name)"
        $count++
    }

    Write-Host ""
    Write-Info "Installed $count files to $ClaudeHome"
}

function Do-Dry {
    Write-Info "Dry run — would install to: $ClaudeHome"
    Write-Host ""

    Write-Host "Agents:"
    Get-ChildItem "$AgentsSrc\*.md" | ForEach-Object {
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
    Write-Host "Commands:"
    Get-ChildItem "$CommandsSrc\*.md" | ForEach-Object {
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
}

function Do-Diff {
    Write-Info "Comparing repo <-> installed ($ClaudeHome)"
    $hasDiff = $false

    $pairs = @(
        @{ Label = "agents"; Src = $AgentsSrc; Dst = $AgentsDst },
        @{ Label = "commands"; Src = $CommandsSrc; Dst = $CommandsDst }
    )

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
                Write-Warn "$($pair.Label)/$($_.Name) — not installed"
                $hasDiff = $true
            }
        }
    }

    if (-not $hasDiff) {
        Write-Ok "Everything in sync"
    }
}

function Do-Pull {
    Write-Info "Pulling installed versions back to repo"
    $count = 0

    Get-ChildItem "$AgentsDst\*.md" -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item $_.FullName -Destination (Join-Path $AgentsSrc $_.Name) -Force
        Write-Ok "agents/$($_.Name) <- installed"
        $count++
    }

    Get-ChildItem "$CommandsDst\*.md" -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item $_.FullName -Destination (Join-Path $CommandsSrc $_.Name) -Force
        Write-Ok "commands/$($_.Name) <- installed"
        $count++
    }

    Write-Host ""
    Write-Info "Pulled $count files into repo"
}

# Main
if ($Help) {
    Get-Help $MyInvocation.MyCommand.Path -Detailed
    exit 0
}

if ($Dry) { Do-Dry }
elseif ($Diff) { Do-Diff }
elseif ($Pull) { Do-Pull }
else { Do-Install }
