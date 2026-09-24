@echo off
cd /d "%~dp0.."
start "" pythonw.exe "%~dp0..\dashboard\backend\control_panel.py" --model deepseek
