@echo off
cd /d "%~dp0"
start "" "%~dp0dashboard.bat"
python -u "%~dp0yuanbao_loop.py" --questions-file "%~dp0product.txt" --rounds 300 --collect-web --max-retries 3
pause
