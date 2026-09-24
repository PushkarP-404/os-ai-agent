<#
.SYNOPSIS
    Boots the AI-Agent OS appliance image in QEMU.
.DESCRIPTION
    Launches QEMU x86_64 with 4GB RAM, 4 vCPUs, and boots into AI-Agent OS v0.1.
    All system services (llama-server, ai-agent, sshd) initialize automatically.
    SSH access is forwarded to localhost:2222 (user: root, pass: aPushkar@12784).
#>
param(
    [string]$Image = "ai-agent-os-v0.1.qcow2",
    [string]$QemuExe = "C:\Program Files\qemu\qemu-system-x86_64.exe",
    [int]$MemoryMB = 4096,
    [int]$Cores = 4,
    [int]$SshPort = 2222
)

if (-not (Test-Path $Image)) {
    if (Test-Path "..\$Image") {
        $Image = "..\$Image"
    } elseif (Test-Path "C:\qemu-alpine\$Image") {
        $Image = "C:\qemu-alpine\$Image"
    } else {
        Write-Error "Image file '$Image' not found."
        exit 1
    }
}

if (-not (Test-Path $QemuExe)) {
    $cmd = Get-Command qemu-system-x86_64 -ErrorAction SilentlyContinue
    if ($cmd) {
        $QemuExe = $cmd.Source
    } else {
        Write-Error "QEMU executable not found at '$QemuExe' or in system PATH."
        exit 1
    }
}

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "       Starting AI-Agent OS Appliance v0.1        " -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  Image:    $Image"
Write-Host "  RAM:      $MemoryMB MB"
Write-Host "  vCPUs:    $Cores"
Write-Host "  SSH Port: $SshPort (root / aPushkar@12784)"
Write-Host "--------------------------------------------------" -ForegroundColor Gray

& $QemuExe `
    -m "$($MemoryMB)M" `
    -smp $Cores `
    -hda "$Image" `
    -boot c `
    -net "user,hostfwd=tcp:127.0.0.1:$($SshPort)-:22" `
    -net nic `
    -serial stdio `
    -nographic
