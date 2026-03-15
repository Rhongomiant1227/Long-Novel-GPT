@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_daily_fanqie_uploads_once.ps1" %*
exit /b %ERRORLEVEL%
