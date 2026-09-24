$ErrorActionPreference = "Stop"
$TaskRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DefaultSync = Join-Path (Split-Path $TaskRoot -Parent) "runtime\remote_workers\deepseek_sync.json"
if (-not $env:DEEPSEEK_REMOTE_SYNC_CONFIG -and (Test-Path -LiteralPath $DefaultSync)) {
    $env:DEEPSEEK_REMOTE_SYNC_CONFIG = $DefaultSync
}
$ReceiverUrl = "http://127.0.0.1:8766/api/health"
$ReceiverReady = $false
try {
    $Health = Invoke-RestMethod -Uri $ReceiverUrl -TimeoutSec 2
    $ReceiverReady = [bool]$Health.ok
} catch {}

if (-not $ReceiverReady) {
    $Python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $Python) { $Python = Get-Command py -ErrorAction SilentlyContinue }
    if (-not $Python) { throw "Python 3 was not found." }
    $LogDir = Join-Path $TaskRoot "data"
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    Start-Process -FilePath $Python.Source -ArgumentList @("-X", "utf8", (Join-Path $TaskRoot "local_receiver.py")) -WorkingDirectory $TaskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "receiver.log") -RedirectStandardError (Join-Path $LogDir "receiver_error.log")
    $Deadline = (Get-Date).AddSeconds(12)
    do {
        Start-Sleep -Milliseconds 400
        try {
            $Health = Invoke-RestMethod -Uri $ReceiverUrl -TimeoutSec 2
            $ReceiverReady = [bool]$Health.ok
        } catch {}
    } while (-not $ReceiverReady -and (Get-Date) -lt $Deadline)
    if (-not $ReceiverReady) { throw "Local receiver failed to start. Check data\receiver_error.log." }
}

Write-Output "DeepSeek local receiver is ready at http://127.0.0.1:8766"
