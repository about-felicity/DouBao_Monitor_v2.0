@echo off
setlocal
cd /d "%~dp0"

if exist "%APPDATA%\npm\appium.cmd" set "PATH=%APPDATA%\npm;%PATH%"

set "PYTHON_EXE=python"
set "PYTHONW_EXE=pythonw"
if exist "%LocalAppData%\Python\pythoncore-3.14-64\python.exe" set "PYTHON_EXE=%LocalAppData%\Python\pythoncore-3.14-64\python.exe"
if exist "%LocalAppData%\Python\pythoncore-3.14-64\pythonw.exe" set "PYTHONW_EXE=%LocalAppData%\Python\pythoncore-3.14-64\pythonw.exe"
if exist "%~dp0portable_runtime\Python\python.exe" set "PYTHON_EXE=%~dp0portable_runtime\Python\python.exe"
if exist "%~dp0portable_runtime\Python\pythonw.exe" set "PYTHONW_EXE=%~dp0portable_runtime\Python\pythonw.exe"

"%PYTHON_EXE%" -c "import PIL, requests, websocket, lxml, openpyxl" >nul 2>nul
if errorlevel 1 (
  "%PYTHON_EXE%" -m pip install -r "%~dp0requirements.txt"
  if errorlevel 1 (
    echo Python dependency installation failed.
    pause
    exit /b 3
  )
)

start "" "%PYTHONW_EXE%" "%~dp0doubao_mumu_control_panel.py"
exit /b 0
