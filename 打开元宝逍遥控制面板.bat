@echo off
cd /d "%~dp0"
python remote_model_control_panel.py --model yuanbao
if errorlevel 1 pause
