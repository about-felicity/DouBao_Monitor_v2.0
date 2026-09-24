@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo [1/2] Starting LAN receiver and dashboard...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_main_receiver.ps1" -Background -NoOpenDashboard
if errorlevel 1 (
  echo Main receiver startup failed.
  pause
  exit /b 4
)

echo [2/2] Opening dashboard and operation panel...
start "" "http://127.0.0.1:8765/"
call "%~dp0open_control_panel.cmd"
if errorlevel 1 (
  echo Operation panel startup failed.
  pause
  exit /b 5
)
echo The panel will open debug Chrome and enable Start only when both accounts match.
exit /b 0
