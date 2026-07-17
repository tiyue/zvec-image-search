@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\scripts\zvec.ps1" %*
exit /b %ERRORLEVEL%
