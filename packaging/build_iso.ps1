<#
.SYNOPSIS
    Builds a bootable Elenchus OS hybrid BIOS/UEFI Live ISO via WSL2 Ubuntu.
.DESCRIPTION
    Converts ai-agent-os-v0.1.qcow2 into a compressed squashfs + overlayfs Live ISO
    (elenchus-os-v0.1-x86_64.iso) containing Linux 6.6.142-ai-agent, XFCE4,
    the Rust/Slint Sentinel Control Center UI, llama-server, GGUF models, and Lightpanda.
#>
param(
    [string]$InputQcow2 = "",
    [string]$OutputIso = "",
    [string]$WslDistro = "Ubuntu"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir
$WorkspaceRoot = Split-Path -Parent $RepoRoot

if (-not $InputQcow2) { $InputQcow2 = Join-Path $WorkspaceRoot "ai-agent-os-v0.1.qcow2" }
if (-not $OutputIso)  { $OutputIso  = Join-Path $WorkspaceRoot "elenchus-os-v0.1-x86_64.iso" }

function Convert-ToWslPath([string]$WinPath) {
    $full = [System.IO.Path]::GetFullPath($WinPath)
    $drive = $full.Substring(0, 1).ToLower()
    $rest = $full.Substring(2).Replace("\", "/")
    return "/mnt/$drive$rest"
}

$WslBuildSh = Convert-ToWslPath (Join-Path $ScriptDir "build_iso.sh")
$WslQcow2   = Convert-ToWslPath $InputQcow2
$WslIso     = Convert-ToWslPath $OutputIso

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  Elenchus OS - ISO Pipeline Launcher (WSL2)" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  Input QCOW2: $InputQcow2"
Write-Host "  Output ISO:  $OutputIso"

# Normalize line endings on-the-fly without mutating tracked build_iso.sh
$cmd = "tr -d '\r' < '$WslBuildSh' > /tmp/elenchus_build_iso.sh && bash /tmp/elenchus_build_iso.sh '$WslQcow2' '$WslIso'"
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& wsl -d $WslDistro -u root bash -c $cmd
$wslExit = $LASTEXITCODE
$ErrorActionPreference = $prevEAP
if ($wslExit -ne 0) {
    throw "ISO build pipeline failed with exit code $wslExit"
}

Write-Host "`n[OK] Bootable ISO generated: $OutputIso" -ForegroundColor Green
