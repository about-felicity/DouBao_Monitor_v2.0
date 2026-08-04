@echo off
chcp 65001 >nul
title 豆包实时回传日志
cd /d "%~dp0"
echo 正在实时显示远端豆包回传；关闭此窗口不会影响接收服务。
echo 面板入口：http://127.0.0.1:3000 （采集控制 - 豆包）
echo.
powershell -NoProfile -NoExit -Command "Get-Content -LiteralPath '%~dp0doubao_mumu_controller\doubao_lan_receiver.log' -Encoding UTF8 -Tail 80 -Wait"
