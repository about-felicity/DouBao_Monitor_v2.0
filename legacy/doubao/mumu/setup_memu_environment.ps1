[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$controllerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$requirements = Join-Path $controllerDir 'requirements.txt'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

function Refresh-ProcessPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Find-CommandPath([string[]]$Names, [string[]]$Candidates = @()) {
    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) { return $command.Source }
    }
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }
    return $null
}

function Install-WithWinget([string]$Id, [string]$Label) {
    $winget = Find-CommandPath @('winget.exe')
    if (-not $winget) {
        throw "缺少 $Label，且系统没有 winget。请手动安装 $Label 后重新运行。"
    }
    Write-Host "正在安装 $Label ..." -ForegroundColor Cyan
    & $winget install --id $Id --exact --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "$Label 安装失败，winget 退出码：$LASTEXITCODE" }
    Refresh-ProcessPath
}

Set-Location -LiteralPath $controllerDir
Write-Host '1/5 检查 Python...' -ForegroundColor Cyan
$python = Find-CommandPath @() @(
    "$controllerDir\portable_runtime\Python\python.exe"
)
if (-not $python) {
    $python = Find-CommandPath @('python.exe') @(
    "$env:LocalAppData\Programs\Python\Python312\python.exe",
    "$env:LocalAppData\Python\pythoncore-3.14-64\python.exe"
    )
}
if (-not $python) {
    Install-WithWinget 'Python.Python.3.12' 'Python 3.12'
    $python = Find-CommandPath @('python.exe') @("$env:LocalAppData\Programs\Python\Python312\python.exe")
}
if (-not $python) { throw 'Python 已安装但未找到 python.exe，请重启电脑后重试。' }
& $python -m pip install --disable-pip-version-check -r $requirements
if ($LASTEXITCODE -ne 0) { throw "Python 依赖安装失败，退出码：$LASTEXITCODE" }

Write-Host '2/5 检查 Node.js 与 Appium...' -ForegroundColor Cyan
$portableAppium = Join-Path $controllerDir 'portable_runtime\NodeJS\node_modules\appium\build\lib\main.js'
if (Test-Path -LiteralPath $portableAppium -PathType Leaf) {
    Write-Host '已检测到部署包内置 Appium。' -ForegroundColor Green
} else {
    $npm = Find-CommandPath @('npm.cmd') @(
        "$controllerDir\portable_runtime\NodeJS\npm.cmd",
        "$env:ProgramFiles\nodejs\npm.cmd"
    )
    if (-not $npm) {
        Install-WithWinget 'OpenJS.NodeJS.LTS' 'Node.js LTS'
        $npm = Find-CommandPath @('npm.cmd') @("$env:ProgramFiles\nodejs\npm.cmd")
    }
    if (-not $npm) { throw 'Node.js 已安装但未找到 npm.cmd，请重启电脑后重试。' }
    & $npm install --global appium@2.19.0
    if ($LASTEXITCODE -ne 0) { throw "Appium 安装失败，退出码：$LASTEXITCODE" }
    Refresh-ProcessPath
    $appium = Find-CommandPath @('appium.cmd') @("$env:APPDATA\npm\appium.cmd")
    if (-not $appium) { throw 'Appium 已安装但未找到 appium.cmd。' }
    $drivers = (& $appium driver list --installed --json 2>$null) -join "`n"
    if ($drivers -notmatch '"uiautomator2"') {
        & $appium driver install 'uiautomator2@4.2.9'
        if ($LASTEXITCODE -ne 0) { throw "UiAutomator2 驱动安装失败，退出码：$LASTEXITCODE" }
    }
}

Write-Host '3/5 检查 Java...' -ForegroundColor Cyan
$java = Find-CommandPath @('java.exe')
if (-not $java) {
    $java = Get-ChildItem -LiteralPath (Join-Path $controllerDir 'portable_runtime\JavaSDK') -Filter java.exe -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $java) {
    Install-WithWinget 'EclipseAdoptium.Temurin.17.JDK' 'Java 17 JDK'
    $java = Find-CommandPath @('java.exe')
}
if (-not $java) { throw 'Java 已安装但未找到 java.exe，请重启电脑后重试。' }

Write-Host '4/5 检查 Chrome、逍遥和豆包运行环境...' -ForegroundColor Cyan
Write-Host '请确认：逍遥模拟器已启动、豆包已登录，Google Chrome 已安装。' -ForegroundColor Yellow

Write-Host '5/5 执行程序完整自检...' -ForegroundColor Cyan
& $python (Join-Path $controllerDir 'doubao_remote_startup.py') --check-only
if ($LASTEXITCODE -ne 0) { throw "程序环境自检失败，退出码：$LASTEXITCODE" }

Write-Host ''
Write-Host '环境准备完成。现在可双击“打开豆包逍遥控制面板.bat”。' -ForegroundColor Green
