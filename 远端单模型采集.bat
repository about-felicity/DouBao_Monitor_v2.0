@echo off
setlocal
if "%~1"=="" (
  echo Usage: %~nx0 deepseek^|yuanbao^|wenxin^|afu [pairing-file]
  exit /b 1
)
set "PAIRING=%~2"
if not "%PAIRING%"=="" (
  python "%~dp0remote_model_worker.py" --model %~1 --pairing "%PAIRING%" --configure-only
  if errorlevel 1 exit /b %errorlevel%
)
python "%~dp0remote_model_worker.py" --model %~1
