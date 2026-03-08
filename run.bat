@echo off
setlocal
cd /d "%~dp0"

echo Starting Long-Novel-GPT...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
set "EXIT_CODE=%ERRORLEVEL%"

echo.

if "%EXIT_CODE%"=="0" (
  echo Long-Novel-GPT started successfully.
  echo Frontend: http://127.0.0.1:8000
  echo Backend : http://127.0.0.1:7869
  echo You can close this window. The services will keep running.
  echo Press any key to close this window.
  pause ^>nul
) else (
  echo.
  echo Startup failed with exit code %EXIT_CODE%.
  echo Press any key to close this window.
  pause >nul
)

exit /b %EXIT_CODE%
