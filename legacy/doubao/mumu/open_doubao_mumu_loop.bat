@echo off
chcp 65001 >nul
cd /d "%~dp0"

python "%~dp0doubao_mumu_loop.py" ^
  --question "推荐一款染发剂" ^
  --rounds 10 ^
  --min-wait 8 ^
  --stable-seconds 5 ^
  --answer-timeout 180 ^
  --max-round-retries 0

pause
