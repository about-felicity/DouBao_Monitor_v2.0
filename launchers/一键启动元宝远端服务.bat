@echo off
cd /d "%~dp0"
title Yuanbao Remote Collector

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_yuanbao_remote.ps1"
if errorlevel 1 (
    echo.
    echo Startup failed. Check runtime\unified_control\yuanbao_grab.err.log.
    pause
    exit /b 1
)

echo.
echo Yuanbao remote collector is running.
ping 127.0.0.1 -n 4 >nul
exit /b 0
