@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-zvec-command.ps1" %*
exit /b %ERRORLEVEL%
