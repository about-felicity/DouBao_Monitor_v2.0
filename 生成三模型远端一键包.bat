@echo off
cd /d "%~dp0"
python build_remote_model_packages.py --model all
if errorlevel 1 (
  echo Failed to build remote model packages.
  pause
  exit /b 1
)
echo Packages are ready in remote_model_deploy_three_models.
pause
