@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=python"
if exist "%LocalAppData%\Python\pythoncore-3.14-64\python.exe" set "PYTHON_EXE=%LocalAppData%\Python\pythoncore-3.14-64\python.exe"
if exist "%~dp0portable_runtime\Python\python.exe" set "PYTHON_EXE=%~dp0portable_runtime\Python\python.exe"

"%PYTHON_EXE%" "%~dp0doubao_mumu_scheduled_job.py" --config "%~dp0doubao_mumu_panel_config.json"
exit /b %ERRORLEVEL%
