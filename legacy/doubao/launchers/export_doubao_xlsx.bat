@echo off
cd /d "%~dp0.."
python tools\maintenance\export_doubao_xlsx.py
pause
