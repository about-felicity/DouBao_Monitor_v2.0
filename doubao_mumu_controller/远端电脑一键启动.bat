@echo off
call "%~dp0remote_one_click.cmd" --panel-only %*
exit /b %ERRORLEVEL%
