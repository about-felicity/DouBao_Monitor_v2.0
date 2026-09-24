@echo off
setlocal
if "%~1"=="" (
  echo Usage: %~nx0 deepseek^|yuanbao^|wenxin [pairing-file] [rounds]
  exit /b 1
)
set "PAIRING=%~2"
set "ROUNDS=%~3"
if "%ROUNDS%"=="" set "ROUNDS=1000000"
if not "%PAIRING%"=="" (
  python "%~dp0..\transport\remote_worker.py" --model %~1 --pairing "%PAIRING%" --configure-only
  if errorlevel 1 exit /b %errorlevel%
)
python "%~dp0..\transport\remote_worker.py" --model %~1 --rounds %ROUNDS%
