$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

if (-not $isAdmin) {
    $process = Start-Process `
        -FilePath "powershell.exe" `
        -Verb RunAs `
        -Wait `
        -PassThru `
        -ArgumentList @(
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "`"$PSCommandPath`""
        )
    exit $process.ExitCode
}

$firewallScript = Join-Path $PSScriptRoot "configure_lan_firewall.ps1"
& $firewallScript
