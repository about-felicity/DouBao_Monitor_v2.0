@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0doubao_remote_sync_config.json" (
  echo [ERROR] Remote sync is not configured.
  echo Copy doubao_lan_pairing.json from the receiver computer and run the pairing setup first.
  pause
  exit /b 4
)

call "%~dp0remote_one_click.cmd" --capture-only
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo [ERROR] Doubao collector stopped with exit code %EXIT_CODE%.
  pause
)

exit /b %EXIT_CODE%
