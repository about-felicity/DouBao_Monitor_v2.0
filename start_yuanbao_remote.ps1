param(
    [int]$Rounds = 0,
    [ValidateSet("", "interleaved", "sequential")]
    [string]$QuestionMode = ""
)

$ErrorActionPreference = "Stop"

# Start-Process can reject an environment containing both Path and PATH.
$processPath = [Environment]::GetEnvironmentVariable("PATH", "Process")
[Environment]::SetEnvironmentVariable("Path", $null, "Process")
[Environment]::SetEnvironmentVariable("PATH", $processPath, "Process")

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeRoot = Join-Path $projectRoot "runtime\unified_control"
$panelConfig = Join-Path $projectRoot "runtime\remote_workers\yuanbao_panel.json"
$workerScript = Join-Path $projectRoot "remote_model_worker.py"
$stdoutLog = Join-Path $runtimeRoot "yuanbao_grab.out.log"
$stderrLog = Join-Path $runtimeRoot "yuanbao_grab.err.log"
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

function Get-YuanbaoProcesses {
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and (
            $_.CommandLine -match "remote_model_worker\.py.*--model\s+yuanbao" -or
            $_.CommandLine -match "yuanbao_loop\.py"
        )
    })
}

function Test-YuanbaoDevice {
    $adb = "C:\Program Files\Microvirt\MEmu\adb.exe"
    if (-not (Test-Path $adb)) { $adb = "C:\Program Files\Netease\MuMu\nx_main\adb.exe" }
    if (-not (Test-Path $adb)) { return $false }
    $devices = (& $adb devices 2>$null) -join "`n"
    return $devices -match "127\.0\.0\.1:\d+\s+device"
}

function Start-YuanbaoEmulator {
    if (Test-YuanbaoDevice) { return }
    $memuc = "C:\Program Files\Microvirt\MEmu\memuc.exe"
    if (Test-Path $memuc) {
        Write-Host "Starting MEmu instance 0 for Yuanbao..."
        & $memuc start -i 0 | Out-Null
        $deadline = (Get-Date).AddMinutes(3)
        do {
            Start-Sleep -Seconds 3
            $running = (& $memuc listvms --running 2>$null) -join "`n"
        } while ($running -notmatch '^0,' -and (Get-Date) -lt $deadline)
        if ($running -notmatch '^0,') { throw "MEmu instance 0 did not become ready." }
        return
    }
    $cli = "C:\Program Files\Netease\MuMu\nx_main\mumu-cli.exe"
    $adb = "C:\Program Files\Netease\MuMu\nx_main\adb.exe"
    if (-not (Test-Path $cli)) { throw "MuMu mumu-cli.exe was not found." }
    Write-Host "Starting MuMu instance 0 for Yuanbao..."
    & $cli control --vmindex 0 launch | Out-Null
    $deadline = (Get-Date).AddMinutes(3)
    do {
        Start-Sleep -Seconds 3
        if (Test-Path $adb) { & $adb connect 127.0.0.1:16384 2>$null | Out-Null }
    } while (-not (Test-YuanbaoDevice) -and (Get-Date) -lt $deadline)
    if (-not (Test-YuanbaoDevice)) {
        throw "MuMu instance 0 started, but Yuanbao ADB 127.0.0.1:16384 is still offline."
    }
}

$existing = Get-YuanbaoProcesses
if ($existing.Count -gt 0) {
    if (Test-YuanbaoDevice) {
        $ids = ($existing | Select-Object -ExpandProperty ProcessId) -join ", "
        Write-Host "Yuanbao remote collector is already running (PID: $ids)."
        exit 0
    }
    Write-Warning "Yuanbao processes exist but MuMu is offline; restarting the stale collector."
    foreach ($item in ($existing | Sort-Object ProcessId -Descending)) {
        Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

Start-YuanbaoEmulator

if (($Rounds -le 0 -or -not $QuestionMode) -and (Test-Path $panelConfig)) {
    try {
        $saved = Get-Content -Raw -Encoding UTF8 $panelConfig | ConvertFrom-Json
        if ($Rounds -le 0 -and [int]$saved.rounds -gt 0) {
            $Rounds = [int]$saved.rounds
        }
        if (-not $QuestionMode -and $saved.question_mode -in @("interleaved", "sequential")) {
            $QuestionMode = [string]$saved.question_mode
        }
    }
    catch {
        Write-Warning "Could not read yuanbao_panel.json; using defaults."
    }
}

if ($Rounds -le 0) { $Rounds = 100 }
if ($Rounds -gt 10000) { $Rounds = 10000 }
if (-not $QuestionMode) { $QuestionMode = "interleaved" }

Write-Host "Running Yuanbao preflight..."
& python $workerScript --model yuanbao --preflight
if ($LASTEXITCODE -ne 0) {
    throw "Yuanbao preflight failed. Check MuMu, Python dependencies and sync configuration."
}

Write-Host "Starting Yuanbao collector: rounds per question=$Rounds, mode=$QuestionMode"
$process = Start-Process -FilePath "python" `
    -ArgumentList @("-u", $workerScript, "--model", "yuanbao", "--rounds", $Rounds, "--question-mode", $QuestionMode) `
    -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -WindowStyle Hidden `
    -PassThru

$deadline = (Get-Date).AddSeconds(90)
do {
    Start-Sleep -Seconds 2
    $process.Refresh()
    if ($process.HasExited) {
        $details = ""
        if (Test-Path $stderrLog) {
            $details = (Get-Content $stderrLog -Tail 12 -ErrorAction SilentlyContinue) -join [Environment]::NewLine
        }
        throw "Yuanbao collector exited during startup (code $($process.ExitCode)).`n$details"
    }
    $collector = @(Get-YuanbaoProcesses | Where-Object { $_.CommandLine -match "yuanbao_loop\.py" })
} while ($collector.Count -eq 0 -and (Get-Date) -lt $deadline)

if ($collector.Count -eq 0) {
    throw "Yuanbao collector startup timed out. Check $stderrLog"
}

Write-Host "Yuanbao remote collector is ready (worker PID $($process.Id), collector PID $($collector[0].ProcessId))."
Write-Host "Logs: $stdoutLog"
