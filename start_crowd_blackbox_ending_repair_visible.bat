@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\run_visible_ending_repair.ps1" -ProjectTag "crowd_blackbox"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
  echo Visible ending repair finished successfully.
) else (
  if "%EXIT_CODE%"=="2" (
    echo Visible ending repair paused for manual review.
  ) else (
    echo Visible ending repair stopped with exit code %EXIT_CODE%.
  )
)
echo Press any key to close this window.
pause >nul
exit /b %EXIT_CODE%
