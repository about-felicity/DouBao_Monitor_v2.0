@echo off
setlocal
cd /d "%~dp0"
set "CONTROLLER_DIR=%~dp0doubao_mumu_controller"
set "PANEL_SCRIPT=%CONTROLLER_DIR%\launch_control_panel.py"
if not exist "%PANEL_SCRIPT%" (
  echo ERROR: The repository is incomplete.
  echo Missing: %PANEL_SCRIPT%
  echo Download and extract the entire GitHub repository, not only this CMD file.
  pause
  exit /b 2
)

set "PANEL_PYTHON=python"
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PANEL_PYTHON=%LocalAppData%\Programs\Python\Python312\python.exe"
if exist "%LocalAppData%\Python\pythoncore-3.14-64\python.exe" set "PANEL_PYTHON=%LocalAppData%\Python\pythoncore-3.14-64\python.exe"
if exist "%CONTROLLER_DIR%\portable_runtime\Python\python.exe" set "PANEL_PYTHON=%CONTROLLER_DIR%\portable_runtime\Python\python.exe"

"%PANEL_PYTHON%" --version >nul 2>nul
if errorlevel 1 (
  echo Python is missing. Starting the first-time environment setup...
  call "%~dp0setup_memu_capture.cmd"
  if errorlevel 1 exit /b 3
  set "PANEL_PYTHON=python"
  if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PANEL_PYTHON=%LocalAppData%\Programs\Python\Python312\python.exe"
  if exist "%LocalAppData%\Python\pythoncore-3.14-64\python.exe" set "PANEL_PYTHON=%LocalAppData%\Python\pythoncore-3.14-64\python.exe"
  if exist "%CONTROLLER_DIR%\portable_runtime\Python\python.exe" set "PANEL_PYTHON=%CONTROLLER_DIR%\portable_runtime\Python\python.exe"
)

"%PANEL_PYTHON%" "%PANEL_SCRIPT%"
set "PANEL_EXIT=%ERRORLEVEL%"
if not "%PANEL_EXIT%"=="0" (
  echo Control panel startup failed. Exit code: %PANEL_EXIT%
  echo Run setup_memu_capture.cmd first and send a screenshot of this window if it still fails.
  pause
)
exit /b %PANEL_EXIT%
