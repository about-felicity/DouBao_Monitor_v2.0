@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0doubao_remote_sync_config.json" (
  echo 尚未配置主电脑回传地址。
  echo 请先把主电脑的 doubao_lan_pairing.json 拖到“远端豆包一键配置回传.bat”。
  echo 配置成功后，再双击本文件开始采集。
  pause
  exit /b 4
)
call "%~dp0remote_one_click.cmd" --capture-only
exit /b %ERRORLEVEL%
