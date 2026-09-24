$ErrorActionPreference = "Stop"
$TaskRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
python (Join-Path $TaskRoot "tests\test_receiver.py")
if ($LASTEXITCODE -ne 0) { throw "Python tests failed" }
python (Join-Path $TaskRoot "tests\test_remote_sync.py")
if ($LASTEXITCODE -ne 0) { throw "Remote sync tests failed" }
$ChromeCandidates = @(
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
    (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
)
$Chrome = $ChromeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $Chrome) { throw "Chrome not found" }
$TestPage = (New-Object System.Uri((Join-Path $TaskRoot "tests\extension_self_test.html"))).AbsoluteUri
$Profile = Join-Path ([IO.Path]::GetTempPath()) ("deepseek-monitor-test-" + [guid]::NewGuid().ToString("N"))
$Output = Join-Path ([IO.Path]::GetTempPath()) ("deepseek-monitor-dom-" + [guid]::NewGuid().ToString("N") + ".txt")
$ErrorLog = $Output + ".err"
$ExtensionDir = Join-Path $TaskRoot "extension"
$Process = Start-Process -FilePath $Chrome -ArgumentList @('--headless=new','--disable-gpu','--no-first-run',"--user-data-dir=$Profile", "--disable-extensions-except=$ExtensionDir", "--load-extension=$ExtensionDir", '--allow-file-access-from-files','--dump-dom',$TestPage) -NoNewWindow -Wait -PassThru -RedirectStandardOutput $Output -RedirectStandardError $ErrorLog
$Dom = Get-Content -Raw -ErrorAction SilentlyContinue -LiteralPath $Output
if ($Process.ExitCode -ne 0 -or $Dom -notmatch "ALL_PASS") { throw "Extension self test failed: $Dom" }
Write-Output "Extension self test: ALL_PASS"
$TempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$ResolvedProfile = [IO.Path]::GetFullPath($Profile)
if ($ResolvedProfile.StartsWith($TempRoot, [StringComparison]::OrdinalIgnoreCase)) {
    Remove-Item -LiteralPath $ResolvedProfile -Recurse -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $Output,$ErrorLog -Force -ErrorAction SilentlyContinue
