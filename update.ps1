#Requires -Version 5.1
<#
.SYNOPSIS
    agentpipe — Update Script (canonical update entry point) (Windows PowerShell).
.DESCRIPTION
    Equivalent to `.\install.ps1 -Update`. Forwards supported install args.
.EXAMPLE
    .\update.ps1                          # update for Claude Code
    .\update.ps1 -Target codex            # update for Codex CLI
    .\update.ps1 -NoClaudeMd              # skip baseline CLAUDE.md install on update
    .\update.ps1 -WithSoundHooks          # opt-in: Stop sound hook only during update
    .\update.ps1 -WithNotificationSound   # opt-in: Claude Notification sound hook only during update
#>
param(
    [ValidateSet("claude", "codex")]
    [string]$Target = "claude",
    [switch]$Dry,
    [switch]$Diff,
    [switch]$Pull,
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
    [string]$ModelProfile = "",
    [switch]$ShowVersion,
    [switch]$Help
)

$ErrorActionPreference = "Stop"
$ScriptDir = $PSScriptRoot
$Install = Join-Path $ScriptDir "install.ps1"

# Forward to install.ps1 -Update with named-parameter splatting. Array splatting
# would pass strings like "-Target" positionally instead of binding them by name.
$forwardArgs = @{}
foreach ($key in $PSBoundParameters.Keys) {
    $forwardArgs[$key] = $PSBoundParameters[$key]
}
$forwardArgs["Update"] = $true
& $Install @forwardArgs
exit $LASTEXITCODE
