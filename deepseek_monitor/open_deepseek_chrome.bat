@echo off
setlocal
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo Google Chrome was not found.
  pause
  exit /b 1
)
start "DeepSeek Monitor" "%CHROME%" --remote-debugging-port=9333 --remote-allow-origins=* --no-first-run --no-default-browser-check --user-data-dir="%~dp0chrome_profile" "https://chat.deepseek.com/"
