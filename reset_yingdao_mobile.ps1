$ErrorActionPreference = "Stop"

$shadowbotRoot = "C:\Program Files\ShadowBot\shadowbot-6.2.21"
$adb = "C:\ProgramData\ShadowBot\support_x64\mobile\AndroidSDK\platform-tools\adb.exe"
$deviceId = "29Q0223812052356"

function Stop-TargetProcess {
    param(
        [string]$Name,
        [string]$CommandContains = ""
    )

    $processes = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq $Name -and (
            [string]::IsNullOrWhiteSpace($CommandContains) -or
            ($_.CommandLine -like "*$CommandContains*")
        )
    }

    foreach ($process in $processes) {
        try {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
            Write-Host "STOPPED $($process.Name) PID=$($process.ProcessId)"
        } catch {
            Write-Warning "FAILED TO STOP $($process.Name) PID=$($process.ProcessId): $($_.Exception.Message)"
        }
    }
}

Write-Host "== stop stale ShadowBot mobile processes =="
Stop-TargetProcess -Name "ShadowBot.MobileScreen.exe"
Stop-TargetProcess -Name "ShadowBot.Mobile.Provider.exe"
Stop-TargetProcess -Name "ShadowBot.Shell.MobileDeviceManager.exe"
Stop-TargetProcess -Name "node.exe" -CommandContains "appium\build\lib\main.js -p 4723"
Stop-TargetProcess -Name "node.exe" -CommandContains "appium\build\lib\main.js -p 4725"

Write-Host "== clear adb forward and phone-side appium services =="
& $adb -s $deviceId forward --remove tcp:8200 2>$null | Out-Null
& $adb -s $deviceId shell am force-stop io.appium.uiautomator2.server | Out-Null
& $adb -s $deviceId shell am force-stop io.appium.uiautomator2.server.test | Out-Null
& $adb -s $deviceId shell am force-stop io.appium.settings | Out-Null
& $adb -s $deviceId shell cmd appops set io.appium.uiautomator2.server RUN_IN_BACKGROUND allow | Out-Null
& $adb -s $deviceId shell cmd appops set io.appium.uiautomator2.server RUN_ANY_IN_BACKGROUND allow | Out-Null
& $adb -s $deviceId shell cmd appops set io.appium.uiautomator2.server.test RUN_IN_BACKGROUND allow | Out-Null
& $adb -s $deviceId shell cmd appops set io.appium.uiautomator2.server.test RUN_ANY_IN_BACKGROUND allow | Out-Null
& $adb -s $deviceId shell cmd appops set io.appium.settings RUN_IN_BACKGROUND allow | Out-Null
& $adb -s $deviceId shell cmd appops set io.appium.settings RUN_ANY_IN_BACKGROUND allow | Out-Null
& $adb -s $deviceId shell am set-inactive io.appium.uiautomator2.server false | Out-Null
& $adb -s $deviceId shell am set-inactive io.appium.uiautomator2.server.test false | Out-Null
& $adb -s $deviceId shell am set-inactive io.appium.settings false | Out-Null
& $adb -s $deviceId shell settings put global stay_on_while_plugged_in 3 | Out-Null
& $adb -s $deviceId shell dumpsys deviceidle disable | Out-Null

Start-Sleep -Seconds 2

Write-Host "== restart mobile bridge =="
Start-Process (Join-Path $shadowbotRoot "ShadowBot.Shell.MobileDeviceManager.exe") -ArgumentList "--reuse=true"

Start-Sleep -Seconds 5

Write-Host "== status =="
try {
    $status4723 = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:4723/wd/hub/status" -TimeoutSec 3
    Write-Host "4723 => $($status4723.Content)"
} catch {
    Write-Host "4723 => not running"
}

try {
    $status4725 = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:4725/wd/hub/status" -TimeoutSec 3
    Write-Host "4725 => $($status4725.Content)"
} catch {
    Write-Host "4725 => waiting for ShadowBot to create session"
}

Write-Host "== adb devices =="
& $adb devices -l

Write-Host "== adb forward =="
& $adb -s $deviceId forward --list

Write-Host "== phone automation processes =="
& $adb -s $deviceId shell ps -A | Select-String "uiautomator|appium|atx"
