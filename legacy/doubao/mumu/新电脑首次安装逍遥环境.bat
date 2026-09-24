@echo off
chcp 65001 >nul
title 豆包逍遥抓取 - 首次环境安装
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_memu_environment.ps1"
set "SETUP_EXIT=%ERRORLEVEL%"
echo.
if "%SETUP_EXIT%"=="0" (
  echo 环境安装和自检成功。
) else (
  echo 环境安装或自检失败，错误码：%SETUP_EXIT%
)
pause
exit /b %SETUP_EXIT%
