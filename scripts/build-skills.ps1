#Requires -Version 5.1
<#
.SYNOPSIS
    Build release archives for every skill in skills/.
.DESCRIPTION
    Produces dist\<skill>.zip with <skill>\ at the top level.
.EXAMPLE
    scripts\build-skills.ps1
    scripts\build-skills.ps1 -Name itmo-report
#>
param(
    [string]$Name = ""
)

$ErrorActionPreference = "Stop"

$ScriptDir  = Split-Path -Parent $PSScriptRoot
if (-not $ScriptDir) { $ScriptDir = (Resolve-Path "$PSScriptRoot\..").Path }
$SkillsSrc  = Join-Path $ScriptDir "skills"
$Dist       = Join-Path $ScriptDir "dist"
$VersionFile = Join-Path $ScriptDir "VERSION"
$Version = if (Test-Path $VersionFile) { (Get-Content $VersionFile -Raw).Trim() } else { "unknown" }

if (-not (Test-Path $SkillsSrc)) {
    Write-Host "No skills/ directory found at $SkillsSrc" -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Path $Dist -Force | Out-Null

$count = 0
Get-ChildItem $SkillsSrc -Directory | ForEach-Object {
    if ($Name -and $_.Name -ne $Name) { return }

    $skillMd = Join-Path $_.FullName "SKILL.md"
    if (-not (Test-Path $skillMd)) {
        Write-Host "skills/$($_.Name)/ has no SKILL.md - skipping" -ForegroundColor Red
        return
    }

    $out = Join-Path $Dist "$($_.Name).zip"
    if (Test-Path $out) { Remove-Item $out }
    Compress-Archive -Path $_.FullName -DestinationPath $out -Force
    Write-Host "  dist/$($_.Name).zip" -ForegroundColor Green
    $count++
}

if ($Name -and $count -eq 0) {
    Write-Host "skill '$Name' not found in skills/" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Built $count skill archive(s) in $Dist (claude-agents v$Version)" -ForegroundColor Cyan
