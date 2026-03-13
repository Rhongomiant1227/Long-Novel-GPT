@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

set "NODE_CMD=node"
where node >nul 2>nul
if errorlevel 1 (
  echo Node.js was not found on PATH.
  echo Press any key to close this window.
  pause >nul
  exit /b 1
)

echo Starting fanqie daily scheduler...
echo Config: %~dp0fanqie_daily_jobs.json
echo.
echo This scheduler stays running and triggers headless uploads at the configured local times.
echo Close this window to stop the scheduler.
echo.

"%NODE_CMD%" scripts\fanqie_daily_scheduler.mjs --config "%~dp0fanqie_daily_jobs.json" %*

set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
  echo Fanqie daily scheduler finished successfully.
) else (
  echo Fanqie daily scheduler stopped with exit code %EXIT_CODE%.
)
echo Press any key to close this window.
pause >nul
exit /b %EXIT_CODE%
