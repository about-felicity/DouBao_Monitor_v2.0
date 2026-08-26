@echo off
setlocal EnableExtensions
chcp 65001 >nul

rem Request administrator rights so MuMu services and elevated VM processes can be stopped.
net session >nul 2>&1
if not "%errorlevel%"=="0" (
    echo 正在申请管理员权限...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo [1/3] 停止 MuMu 服务...
sc.exe stop MuMuRemoteService >nul 2>&1
sc.exe stop MuMuNxService >nul 2>&1
timeout /t 2 /nobreak >nul

echo [2/3] 结束 MuMu 已知进程及安装目录内的残留进程...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$known = 'MuMu','MuMuPlayer','MuMuManager','MuMuMultiPlayer','MuMuNxMain','MuMuNxDevice','MuMuNxService','MuMuNxUpdater','MuMuRemoteService','NemuPlayer','NemuHeadless','NemuService','NemuSVC','NemuVMMS','NemuVMMHeadless';" ^
  "$targets = Get-Process -ErrorAction SilentlyContinue | Where-Object { $known -contains $_.ProcessName -or $_.Path -match '\\(Netease|NetEase)\\MuMu\\' };" ^
  "$targets | ForEach-Object { Write-Host ('  结束 {0} (PID {1})' -f $_.ProcessName,$_.Id); Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue };"

timeout /t 2 /nobreak >nul
echo [3/3] 检查残留...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$known = 'MuMu','MuMuPlayer','MuMuManager','MuMuMultiPlayer','MuMuNxMain','MuMuNxDevice','MuMuNxService','MuMuNxUpdater','MuMuRemoteService','NemuPlayer','NemuHeadless','NemuService','NemuSVC','NemuVMMS','NemuVMMHeadless';" ^
  "$left = Get-Process -ErrorAction SilentlyContinue | Where-Object { $known -contains $_.ProcessName -or $_.Path -match '\\(Netease|NetEase)\\MuMu\\' };" ^
  "if ($left) { Write-Host '仍有进程未能结束：' -ForegroundColor Yellow; $left | Format-Table Id,ProcessName,Path -AutoSize } else { Write-Host 'MuMu 进程已全部清理。' -ForegroundColor Green }"

echo.
echo 提示：此脚本只结束进程，不删除模拟器、账号或采集数据。
pause
endlocal
