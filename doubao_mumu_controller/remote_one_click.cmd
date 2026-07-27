@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=python"
if exist "%LocalAppData%\Python\pythoncore-3.14-64\python.exe" set "PYTHON_EXE=%LocalAppData%\Python\pythoncore-3.14-64\python.exe"
if exist "%~dp0portable_runtime\Python\python.exe" set "PYTHON_EXE=%~dp0portable_runtime\Python\python.exe"

"%PYTHON_EXE%" --version >nul 2>nul
if errorlevel 1 (
  echo Python was not found.
  pause
  exit /b 2
)

"%PYTHON_EXE%" -c "import PIL, requests, websocket, lxml, openpyxl" >nul 2>nul
if errorlevel 1 (
  echo Installing Python dependencies...
  "%PYTHON_EXE%" -m pip install -r "%~dp0requirements.txt"
  if errorlevel 1 (
    echo Python dependency installation failed.
    pause
    exit /b 3
  )
)

"%PYTHON_EXE%" "%~dp0doubao_remote_startup.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Program exit code: %EXIT_CODE%
if not "%EXIT_CODE%"=="0" echo See doubao_remote_startup.log.
if not "%DOUBAO_NO_LAUNCHER_PAUSE%"=="1" pause
exit /b %EXIT_CODE%
