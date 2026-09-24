@echo off
cd /d "%~dp0"
start "dashboard" cmd /c "%~dp0dashboard.bat"
timeout /t 3 /nobreak
python -u "%~dp0yuanbao_loop.py" --questions-file "%~dp0product.txt" --rounds 300 --collect-web --max-retries 3
pause
