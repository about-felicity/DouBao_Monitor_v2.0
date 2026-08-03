param([switch]$NoOpen)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$dashboardRoot = Join-Path $projectRoot "yuanbao_monitor\dashboard"
$runtimeRoot = Join-Path $projectRoot "runtime\unified_control"
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

function Test-ListeningPort([int]$Port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

if (-not (Test-ListeningPort 8790)) {
    $receiverOut = Join-Path $runtimeRoot "doubao_receiver.out.log"
    $receiverErr = Join-Path $runtimeRoot "doubao_receiver.err.log"
    Start-Process -FilePath "python" -ArgumentList @("-u", (Join-Path $projectRoot "doubao_mumu_controller\doubao_lan_receiver.py")) `
        -WorkingDirectory $projectRoot -RedirectStandardOutput $receiverOut -RedirectStandardError $receiverErr -WindowStyle Hidden
}

if (-not (Test-ListeningPort 8765)) {
    $backendOut = Join-Path $runtimeRoot "dashboard_server.out.log"
    $backendErr = Join-Path $runtimeRoot "dashboard_server.err.log"
    Start-Process -FilePath "python" -ArgumentList @("-u", (Join-Path $projectRoot "doubao_dashboard_server.py")) `
        -WorkingDirectory $projectRoot -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr -WindowStyle Hidden
}

if (-not (Test-ListeningPort 3000)) {
    if (-not (Test-Path (Join-Path $dashboardRoot "node_modules"))) {
        & npm install --prefix $dashboardRoot
        if ($LASTEXITCODE -ne 0) { throw "React dashboard dependency installation failed." }
    }
    & python (Join-Path $projectRoot "yuanbao_monitor\build_dashboard_data.py")
    $frontendOut = Join-Path $runtimeRoot "react_dashboard.out.log"
    $frontendErr = Join-Path $runtimeRoot "react_dashboard.err.log"
    Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "npm run dev") -WorkingDirectory $dashboardRoot `
        -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr -WindowStyle Hidden
}

$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    if ((Test-ListeningPort 8765) -and (Test-ListeningPort 3000)) { break }
    Start-Sleep -Milliseconds 500
}
if (-not ((Test-ListeningPort 8765) -and (Test-ListeningPort 3000))) {
    throw "Unified dashboard startup timed out. Check runtime\unified_control logs."
}
if (-not $NoOpen) { Start-Process "http://127.0.0.1:3000" }
Write-Host "Unified Doubao, Yuanbao and DeepSeek React dashboard: http://127.0.0.1:3000"
