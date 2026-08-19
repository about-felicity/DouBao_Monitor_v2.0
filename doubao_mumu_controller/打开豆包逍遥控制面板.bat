@echo off
chcp 65001 >nul
title 豆包逍遥模拟器控制面板
set "PYTHON_EXE=python"
if exist "%~dp0portable_runtime\Python\python.exe" set "PYTHON_EXE=%~dp0portable_runtime\Python\python.exe"
"%PYTHON_EXE%" "%~dp0launch_control_panel.py"
set "PANEL_EXIT=%ERRORLEVEL%"
if not "%PANEL_EXIT%"=="0" (
  echo 控制面板启动失败，错误码：%PANEL_EXIT%
  pause
  exit /b %PANEL_EXIT%
)
exit /b 0
