@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHON_CMD=.venv\Scripts\python.exe"

if not exist "%PYTHON_CMD%" (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python virtualenv not found, and no python was found on PATH.
    echo Please run `run.bat` once first, or install Python.
    echo Press any key to close this window.
    pause >nul
    exit /b 1
  )
  set "PYTHON_CMD=python"
)

if not exist "novel_brief.md" (
  echo novel_brief.md not found.
  if exist "novel_brief.example.md" (
    echo Please copy novel_brief.example.md to novel_brief.md and edit it first.
  ) else (
    echo Please create novel_brief.md first.
  )
  echo Press any key to close this window.
  pause >nul
  exit /b 1
)

if "%GPT_MAX_INPUT_TOKENS%"=="" (
  set "GPT_MAX_INPUT_TOKENS=350000"
)

if "%GPT_MAX_OUTPUT_TOKENS%"=="" (
  set "GPT_MAX_OUTPUT_TOKENS=65536"
)

echo Starting visible auto-novel watchdog...
echo Project: %~dp0auto_projects\default_project
echo Input budget: %GPT_MAX_INPUT_TOKENS%
echo Output budget: %GPT_MAX_OUTPUT_TOKENS%
echo.
echo This window shows live LLM output and will auto-restart on stalls.
echo Close this window to stop the watchdog.
echo.

"%PYTHON_CMD%" watch_auto_novel_visible.py ^
  --project-dir "%~dp0auto_projects\default_project" ^
  --brief-file "%~dp0novel_brief.md" ^
  --target-chars 2000000 ^
  --chapter-char-target 2200 ^
  --chapters-per-volume 30 ^
  --chapters-per-batch 5 ^
  --memory-refresh-interval 5 ^
  --main-model gpt/gpt-5.4 ^
  --sub-model gpt/gpt-5.4 ^
  --planner-reasoning-effort medium ^
  --writer-reasoning-effort medium ^
  --sub-reasoning-effort low ^
  --summary-reasoning-effort low ^
  --max-thread-num 1 ^
  --max-retries 0 ^
  --stall-timeout-seconds 480 ^
  --restart-delay-seconds 15 ^
  --max-stage-runtime-seconds 0 %*

set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
  echo Visible auto-novel watchdog finished successfully.
) else (
  echo Visible auto-novel watchdog stopped with exit code %EXIT_CODE%.
)
echo Press any key to close this window.
pause >nul
exit /b %EXIT_CODE%
