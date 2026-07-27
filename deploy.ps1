$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$controllerRoot = Join-Path $projectRoot "doubao_mumu_controller"

function Resolve-CommandPath([string]$Name, [string[]]$Fallbacks = @()) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($candidate in $Fallbacks) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

function Install-WithWinget([string]$Id, [string]$Label) {
    $winget = Resolve-CommandPath "winget.exe"
    if (-not $winget) {
        throw "$Label is missing and winget is unavailable. Install $Label manually."
    }
    Write-Host "Installing $Label..."
    & $winget install --id $Id --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "$Label installation failed with exit code $LASTEXITCODE."
    }
}

function Invoke-NativeCapture([string]$Executable, [string[]]$Arguments) {
    # Windows PowerShell converts ordinary native stderr messages into
    # NativeCommandError records. Appium writes progress banners to stderr
    # even when the command succeeds, so capture them without letting the
    # global Stop policy abort deployment.
    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    try {
        $process = Start-Process `
            -FilePath $Executable `
            -ArgumentList $Arguments `
            -NoNewWindow `
            -Wait `
            -PassThru `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath
        $stdout = Get-Content -LiteralPath $stdoutPath -Raw -ErrorAction SilentlyContinue
        $stderr = Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue
    }
    finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
    return [pscustomobject]@{
        ExitCode = $process.ExitCode
        Output = ((@($stdout, $stderr) | Where-Object { $_ }) -join [Environment]::NewLine)
    }
}

Set-Location -LiteralPath $projectRoot
Write-Host "[1/6] Checking Python..."
$python = Resolve-CommandPath "python.exe" @(
    (Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe")
)
if (-not $python) {
    Install-WithWinget "Python.Python.3.12" "Python 3.12"
    $python = Resolve-CommandPath "python.exe" @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe")
    )
}
if (-not $python) { throw "Python is still unavailable. Reopen this installer." }

Write-Host "[2/6] Installing Python dependencies..."
& $python -m pip install -r (Join-Path $controllerRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed." }

Write-Host "[3/6] Checking Node.js and Appium..."
$nodeFallback = Join-Path $env:ProgramFiles "nodejs\node.exe"
$npmFallback = Join-Path $env:ProgramFiles "nodejs\npm.cmd"
$node = Resolve-CommandPath "node.exe" @($nodeFallback)
$npm = Resolve-CommandPath "npm.cmd" @($npmFallback)
if (-not $node -or -not $npm) {
    Install-WithWinget "OpenJS.NodeJS.LTS" "Node.js LTS"
    $env:Path = (Join-Path $env:ProgramFiles "nodejs") + ";" + $env:Path
    $node = Resolve-CommandPath "node.exe" @($nodeFallback)
    $npm = Resolve-CommandPath "npm.cmd" @($npmFallback)
}
if (-not $node -or -not $npm) { throw "Node.js is still unavailable. Reopen this installer." }

$appium = Resolve-CommandPath "appium.cmd" @(
    (Join-Path $env:APPDATA "npm\appium.cmd")
)
if (-not $appium) {
    Write-Host "Installing Appium 2..."
    & $npm install --global appium@2
    if ($LASTEXITCODE -ne 0) { throw "Appium installation failed." }
    $appium = Resolve-CommandPath "appium.cmd" @(
        (Join-Path $env:APPDATA "npm\appium.cmd")
    )
}
if (-not $appium) { throw "Appium is still unavailable after installation." }

Write-Host "[4/6] Checking the UiAutomator2 driver..."
$driverList = Invoke-NativeCapture $appium @("driver", "list", "--installed", "--json")
$installedDrivers = $driverList.Output
if ($driverList.ExitCode -ne 0 -or $installedDrivers -notmatch "uiautomator2") {
    $driverInstall = Invoke-NativeCapture $appium @("driver", "install", "uiautomator2")
    if ($driverInstall.Output) { Write-Host $driverInstall.Output.Trim() }
    if ($driverInstall.ExitCode -ne 0) {
        # Some Appium builds return a non-zero exit code when the driver is
        # already present. Verify once more instead of failing the deployment.
        $installedDrivers = (
            Invoke-NativeCapture $appium @("driver", "list", "--installed", "--json")
        ).Output
        if ($installedDrivers -notmatch "uiautomator2") {
            throw "UiAutomator2 driver installation failed."
        }
    }
}

Write-Host "[5/6] Preparing local configuration and desktop software..."
$keyFile = Join-Path $controllerRoot "doubao_api_keys.env"
$keyExample = Join-Path $controllerRoot "doubao_api_keys.env.example"
if (-not (Test-Path -LiteralPath $keyFile)) {
    Copy-Item -LiteralPath $keyExample -Destination $keyFile
    Write-Host "Created the local key file. Add your own API key if AI fallback is required: $keyFile"
}
$chrome = @(
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $chrome) { throw "Google Chrome was not found. Install Chrome first." }
$mumu = @(
    (Join-Path $env:ProgramFiles "Netease\MuMu\nx_main\MuMuManager.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Netease\MuMu\nx_main\MuMuManager.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $mumu) { throw "MuMu was not found. Install and start MuMu first." }

Write-Host "[6/6] Starting the control panel and local dashboard..."
$env:DOUBAO_NO_LAUNCHER_PAUSE = "1"
& (Join-Path $controllerRoot "remote_one_click.cmd") --panel-only
exit $LASTEXITCODE
