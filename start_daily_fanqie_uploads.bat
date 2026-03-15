@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_daily_fanqie_uploads.ps1" %*
exit /b %ERRORLEVEL%
