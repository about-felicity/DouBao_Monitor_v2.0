@echo off
cd /d "%~dp0"
python -u "%~dp0yuanbao_loop.py" --questions-file "%~dp0product.txt" --resume
pause
