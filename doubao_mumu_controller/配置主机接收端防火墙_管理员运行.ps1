$ErrorActionPreference = "Stop"
$ruleName = "Doubao MuMu LAN Receiver 8790"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Start-Process powershell.exe -Verb RunAs -Wait -ArgumentList @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`""
    )
    exit
}

$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if (-not $existing) {
    New-NetFirewallRule `
        -DisplayName $ruleName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort 8790 `
        -Profile Private | Out-Null
    Write-Host "已允许专用网络 TCP 8790 入站。" -ForegroundColor Green
} else {
    Enable-NetFirewallRule -DisplayName $ruleName | Out-Null
    Write-Host "防火墙规则已经存在并已启用。" -ForegroundColor Green
}
Start-Sleep -Seconds 2
