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
if (-not $existing) {
    New-NetFirewallRule `
        -DisplayName $ruleName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort 8790 `
        -Profile Private | Out-Null
} else {
    Set-NetFirewallRule `
        -DisplayName $ruleName `
        -Enabled True `
        -Direction Inbound `
        -Action Allow `
        -Profile Private | Out-Null
}
Write-Host "Firewall rule is ready: Private TCP 8790." `
    -ForegroundColor Green
