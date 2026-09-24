param(
    [switch]$CheckOnly,
    [switch]$Background,
    [switch]$NoOpenDashboard
)

$ErrorActionPreference = "Stop"
$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ruleName = "Doubao MuMu LAN Receiver 8790"
$firewallScript = Join-Path $baseDir "configure_lan_firewall.ps1"
$receiverScript = Join-Path $baseDir "doubao_lan_receiver.py"
if ($CheckOnly) {
    if (-not (Test-Path -LiteralPath $receiverScript)) {
        throw "Receiver script was not found."
    }
    Write-Host "Main receiver launcher check passed."
    exit 0
}

$rules = @(
    Get-NetFirewallRule `
        -DisplayName $ruleName `
        -ErrorAction SilentlyContinue
)
$ruleReady = $rules.Count -eq 1 `
    -and $rules[0].Enabled -eq "True" `
    -and $rules[0].Direction -eq "Inbound" `
    -and $rules[0].Action -eq "Allow" `
    -and $rules[0].Profile -eq "Any"
if (-not $ruleReady) {
    Write-Host "A Windows administrator prompt will appear once."
    $elevated = Start-Process `
        -FilePath "powershell.exe" `
        -Verb RunAs `
        -Wait `
        -PassThru `
        -ArgumentList @(
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "`"$firewallScript`""
        )
    if ($elevated.ExitCode -ne 0) {
        throw "Firewall setup was not approved or failed."
    }
}

$tcp = New-Object System.Net.Sockets.TcpClient
try {
    $async = $tcp.BeginConnect("127.0.0.1", 8790, $null, $null)
    $alreadyRunning = $async.AsyncWaitHandle.WaitOne(800)
    if ($alreadyRunning -and $tcp.Connected) {
        $tcp.EndConnect($async)
        Write-Host "Receiver is already running: http://127.0.0.1:8790"
        if (-not $NoOpenDashboard) {
            Start-Process "http://127.0.0.1:8765/"
            Write-Host "Dashboard opened: http://127.0.0.1:8765/"
        }
        exit 0
    }
} catch {
    $alreadyRunning = $false
} finally {
    $tcp.Dispose()
}

$pythonCandidates = @(
    (Join-Path $baseDir "portable_runtime\Python\python.exe"),
    (Join-Path $env:LocalAppData "Python\pythoncore-3.14-64\python.exe")
)
$pythonCommand = Get-Command "python.exe" -ErrorAction SilentlyContinue
if ($pythonCommand) {
    $pythonCandidates += $pythonCommand.Source
}
$python = $pythonCandidates |
    Where-Object { $_ -and (Test-Path -LiteralPath $_) } |
    Select-Object -First 1
if (-not $python) {
    throw "Python was not found."
}

if ($Background) {
    $pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
    if (-not (Test-Path -LiteralPath $pythonw)) {
        $pythonw = $python
    }
    $receiverProcess = Start-Process `
        -FilePath $pythonw `
        -ArgumentList "`"$receiverScript`"" `
        -WorkingDirectory $baseDir `
        -WindowStyle Hidden `
        -PassThru
    $deadline = (Get-Date).AddSeconds(20)
    $ready = $false
    do {
        Start-Sleep -Milliseconds 300
        if ($receiverProcess.HasExited) {
            throw "Receiver exited during startup with code $($receiverProcess.ExitCode)."
        }
        $probe = New-Object System.Net.Sockets.TcpClient
        $connect = $null
        try {
            $connect = $probe.BeginConnect("127.0.0.1", 8790, $null, $null)
            if ($connect.AsyncWaitHandle.WaitOne(500) -and $probe.Connected) {
                $probe.EndConnect($connect)
                $ready = $true
            } else {
                $ready = $false
            }
        } catch {
            $ready = $false
        } finally {
            if ($connect -and $connect.AsyncWaitHandle) {
                $connect.AsyncWaitHandle.Close()
            }
            $probe.Dispose()
        }
    } until ($ready -or (Get-Date) -gt $deadline)
    if (-not $ready) {
        throw "Receiver did not start within 20 seconds."
    }
    Write-Host "Receiver started in background."
    if (-not $NoOpenDashboard) {
        Start-Process "http://127.0.0.1:8765/"
    }
    exit 0
}

while ($true) {
    & $python $receiverScript
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        exit 0
    }
    Write-Host "Receiver exited with code $exitCode. Restarting in 5 seconds."
    Start-Sleep -Seconds 5
}
