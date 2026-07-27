@echo off
cd /d "%~dp0"
start "" "http://127.0.0.1:8765"
python "%~dp0doubao_dashboard_server.py"
