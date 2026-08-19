@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PANEL_PYTHON=python"
if exist "%~dp0doubao_mumu_controller\portable_runtime\Python\python.exe" set "PANEL_PYTHON=%~dp0doubao_mumu_controller\portable_runtime\Python\python.exe"
"%PANEL_PYTHON%" "%~dp0doubao_mumu_controller\launch_control_panel.py"
if errorlevel 1 (
  echo 逍遥抓取控制面板启动失败，请把此窗口截图发给我。
  pause
  exit /b 1
)
exit /b 0
