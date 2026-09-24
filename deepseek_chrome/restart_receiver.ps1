$ErrorActionPreference = "Stop"
$TaskRoot = [IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$ReceiverScript = [IO.Path]::GetFullPath((Join-Path $TaskRoot "local_receiver.py"))
$DefaultSync = Join-Path (Split-Path $TaskRoot -Parent) "runtime\remote_workers\deepseek_sync.json"
if (-not $env:DEEPSEEK_REMOTE_SYNC_CONFIG -and (Test-Path -LiteralPath $DefaultSync)) {
    $env:DEEPSEEK_REMOTE_SYNC_CONFIG = $DefaultSync
}
$Connection = Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($Connection) {
    $ProcessInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($Connection.OwningProcess)"
    $CommandLine = [string]$ProcessInfo.CommandLine
    if ($ProcessInfo.Name -notmatch "^python" -or $CommandLine.IndexOf($ReceiverScript, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw "Port 8766 is occupied by another process; refusing to stop it."
    }
    Stop-Process -Id $Connection.OwningProcess -Force
    Start-Sleep -Milliseconds 500
}
$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) { $Python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $Python) { throw "Python 3 was not found." }
$LogDir = Join-Path $TaskRoot "data"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Start-Process -FilePath $Python.Source -ArgumentList @("-X", "utf8", $ReceiverScript) -WorkingDirectory $TaskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "receiver.log") -RedirectStandardError (Join-Path $LogDir "receiver_error.log")
$Deadline = (Get-Date).AddSeconds(12)
do {
    Start-Sleep -Milliseconds 400
    try { $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8766/api/health" -TimeoutSec 2 } catch { $Health = $null }
} while (-not $Health.ok -and (Get-Date) -lt $Deadline)
if (-not $Health.ok) { throw "Receiver failed to restart." }
$Health | ConvertTo-Json -Depth 4
