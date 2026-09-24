@echo off
cd /d "%~dp0.."
python dashboard\backend\control_panel.py --model yuanbao
if errorlevel 1 pause
