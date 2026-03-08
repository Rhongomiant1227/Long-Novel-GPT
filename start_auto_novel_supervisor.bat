@echo off
setlocal
cd /d "%~dp0"

echo Starting auto-novel supervisor...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0watch_auto_novel.ps1"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
  echo Auto-novel supervisor stopped normally.
) else (
  echo Auto-novel supervisor stopped with exit code %EXIT_CODE%.
)
echo Press any key to close this window.
pause >nul
exit /b %EXIT_CODE%
