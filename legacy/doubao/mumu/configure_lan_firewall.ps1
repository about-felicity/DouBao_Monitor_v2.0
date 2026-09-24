$ErrorActionPreference = "Stop"
$ruleName = "Doubao MuMu LAN Receiver 8790"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    throw "Administrator permission is required."
}

$existing = Get-NetFirewallRule `
    -DisplayName $ruleName `
    -ErrorAction SilentlyContinue
if ($existing) {
    $existing | Remove-NetFirewallRule
}
New-NetFirewallRule `
    -DisplayName $ruleName `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort 8790 `
    -Profile Any `
    -RemoteAddress LocalSubnet | Out-Null
Write-Host "Firewall rule is ready: local subnet TCP 8790 on any network profile." `
    -ForegroundColor Green
