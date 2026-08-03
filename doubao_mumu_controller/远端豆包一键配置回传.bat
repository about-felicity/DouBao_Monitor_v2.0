@echo off
chcp 65001 >nul
setlocal
if "%~1"=="" (
  echo 请把主电脑生成的 doubao_lan_pairing.json 拖到这个批处理文件上。
  echo.
  pause
  exit /b 2
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0配置远端豆包回传.ps1" -PairingFile "%~1"
if errorlevel 1 (
  echo.
  echo 配置失败，请查看上方错误。
  pause
  exit /b 1
)
echo.
echo 配置完成。之后从远端豆包控制面板启动采集即可自动回传。
pause
