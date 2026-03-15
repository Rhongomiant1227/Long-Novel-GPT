@echo off
setlocal
cd /d "%~dp0"
set "FIRST_ARG=%~1"

if /I "%FIRST_ARG%"=="status" goto run_simple_action
if /I "%FIRST_ARG%"=="enable" goto run_simple_action
if /I "%FIRST_ARG%"=="disable" goto run_simple_action
if /I "%FIRST_ARG%"=="reinstall" goto run_simple_action
if /I "%FIRST_ARG%"=="run" goto run_simple_action
if /I "%FIRST_ARG%"=="run-one" goto run_simple_action

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\manage_fanqie_daily_task.ps1" %*
goto after_run

:run_simple_action
if not "%~2"=="" goto run_named_args
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\manage_fanqie_daily_task.ps1" -Action "%FIRST_ARG%"
goto after_run

:run_named_args
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\manage_fanqie_daily_task.ps1" %*

:after_run
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause >nul
exit /b %EXIT_CODE%
