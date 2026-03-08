@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

for %%F in (".run\backend.pid" ".run\frontend.pid") do (
  if exist %%~F (
    set /p PID=<%%~F
    if defined PID (
      echo Stopping PID !PID! ...
      taskkill /PID !PID! /F >nul 2>&1
    )
    del /f /q %%~F >nul 2>&1
    set "PID="
  )
)

for %%P in (7869 8000) do (
  for /f "tokens=5" %%I in ('netstat -ano ^| findstr /R /C:":%%P .*LISTENING"') do (
    taskkill /PID %%I /F >nul 2>&1
  )
)

echo Done.
exit /b 0
