@echo off
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0doubao_mumu_controller\新电脑首次安装逍遥环境.bat"
exit /b %ERRORLEVEL%
