@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0zvec.ps1" %*
exit /b %ERRORLEVEL%
