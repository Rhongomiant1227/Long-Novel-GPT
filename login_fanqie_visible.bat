@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

set "NODE_CMD=node"
where node >nul 2>&1
if errorlevel 1 (
  echo Node.js was not found in PATH.
  pause
  exit /b 1
)

echo Starting visible Fanqie login repair window...
echo Scan the QR code or finish login in the browser window.
echo Login profile: %~dp0.run\\fanqie_writer_seed_profile
echo The profile is reused by the headless uploader and check-in task.
echo.

"%NODE_CMD%" "%~dp0scripts\\fanqie_login_seed_helper.mjs" ^
  --login-url "https://fanqienovel.com/main/writer/?enter_from=author_zone" ^
  --profile-dir "%~dp0.run\\fanqie_writer_seed_profile" ^
  --screenshot-path "%~dp0.run\\fanqie_login_qr.png" ^
  --status-path "%~dp0.run\\fanqie_login_status.json" ^
  --timeout-seconds 1800

set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
  echo Login detected and saved to the shared seed profile.
) else (
  echo Login helper ended with exit code %EXIT_CODE%.
  echo Status: %~dp0.run\\fanqie_login_status.json
)
echo You can close this window after the login status becomes logged_in.
pause
exit /b %EXIT_CODE%
