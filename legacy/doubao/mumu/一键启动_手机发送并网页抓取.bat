@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=python"
if exist "%LocalAppData%\Python\pythoncore-3.14-64\python.exe" set "PYTHON_EXE=%LocalAppData%\Python\pythoncore-3.14-64\python.exe"

echo [豆包 MuMu 流水线] 正在自动识别设备和账号...
"%PYTHON_EXE%" -c "import PIL, requests, websocket, scrapling" >nul 2>nul
if errorlevel 1 (
  echo 首次运行：正在安装 Python 依赖...
  "%PYTHON_EXE%" -m pip install -r "%~dp0requirements.txt"
)
"%PYTHON_EXE%" "%~dp0doubao_mumu_web_pipeline.py" --questions-file "%~dp0questions.txt"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
  echo 全部问题已发送并完成网页抓取。
) else (
  echo 任务结束，退出码：%EXIT_CODE%
  echo 详情见 doubao_mumu_web_pipeline.log
)
pause
exit /b %EXIT_CODE%
