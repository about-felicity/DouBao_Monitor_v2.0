@echo off
setlocal
cd /d "%~dp0"
set "SETUP_SCRIPT=%~dp0doubao_mumu_controller\setup_memu_environment.ps1"
if not exist "%SETUP_SCRIPT%" (
  echo ERROR: The repository is incomplete.
  echo Missing: %SETUP_SCRIPT%
  echo Download and extract the entire GitHub repository, not only this CMD file.
  pause
  exit /b 2
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SETUP_SCRIPT%"
set "SETUP_EXIT=%ERRORLEVEL%"
if not "%SETUP_EXIT%"=="0" pause
exit /b %SETUP_EXIT%
