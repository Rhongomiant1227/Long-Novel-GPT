@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "LONG_NOVEL_PROJECT_TAG=crowd_blackbox"
set "LONG_NOVEL_USER_AGENT=Long-Novel-GPT/crowd_blackbox"
set "PROJECT_DIR=%~dp0auto_projects\crowd_blackbox"
set "BRIEF_FILE=%PROJECT_DIR%\brief.md"
set "PYTHON_CMD=.venv\Scripts\python.exe"

if not exist "%BRIEF_FILE%" (
  echo brief file not found: %BRIEF_FILE%
  echo Press any key to close this window.
  pause >nul
  exit /b 1
)

if not exist "%PYTHON_CMD%" (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python virtualenv not found, and no python was found on PATH.
    echo Press any key to close this window.
    pause >nul
    exit /b 1
  )
  set "PYTHON_CMD=python"
)

if "%GPT_API_KEY%"=="" (
  for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "$p = Join-Path $env:USERPROFILE '.codex\\auth.json'; if (Test-Path $p) { $json = Get-Content $p -Raw | ConvertFrom-Json; if ($json.OPENAI_API_KEY) { [Console]::Write($json.OPENAI_API_KEY) } }"`) do set "GPT_API_KEY=%%I"
)

if "%GPT_BASE_URL%"=="" (
  set "GPT_BASE_URL=https://fast.vpsairobot.com/v1"
)

if /I "%GPT_BASE_URL%"=="https://vpsairobot.com" (
  set "GPT_BASE_URL=https://vpsairobot.com/v1"
)

if /I "%GPT_BASE_URL%"=="https://fast.vpsairobot.com" (
  set "GPT_BASE_URL=https://fast.vpsairobot.com/v1"
)

if "%GPT_AVAILABLE_MODELS%"=="" (
  set "GPT_AVAILABLE_MODELS=gpt-5.4"
)

if "%GPT_MAX_INPUT_TOKENS%"=="" (
  set "GPT_MAX_INPUT_TOKENS=350000"
)

if "%GPT_MAX_OUTPUT_TOKENS%"=="" (
  set "GPT_MAX_OUTPUT_TOKENS=65536"
)

if "%GPT_API_KEY%"=="" (
  echo GPT_API_KEY is not set and could not be loaded from %%USERPROFILE%%\\.codex\\auth.json.
  echo Press any key to close this window.
  pause >nul
  exit /b 1
)

echo Starting visible auto-novel watchdog...
echo Project: %PROJECT_DIR%
echo Brief: %BRIEF_FILE%
echo Model: gpt/gpt-5.4
echo Base URL: %GPT_BASE_URL%
echo Input budget: %GPT_MAX_INPUT_TOKENS%
echo Output budget: %GPT_MAX_OUTPUT_TOKENS%
echo Retry: infinite
echo Completion mode: min_chars_and_story_end
echo Min chars: 2000000
echo Critic: gpt/gpt-5.4 xhigh on batch tail, unlimited until clean or stalled
echo.
echo This window shows live LLM output and auto-restarts on crashes or stalls.
echo Close this window to stop the watchdog.
echo.

"%PYTHON_CMD%" watch_auto_novel_visible.py ^
  --project-dir "%PROJECT_DIR%" ^
  --brief-file "%BRIEF_FILE%" ^
  --completion-mode min_chars_and_story_end ^
  --target-chars 2000000 ^
  --min-target-chars 2000000 ^
  --max-target-chars 0 ^
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
  --critic-model gpt/gpt-5.4 ^
  --critic-every-chapters 0 ^
  --critic-reasoning-effort xhigh ^
  --critic-max-passes 0 ^
  --max-thread-num 1 ^
  --max-retries 0 ^
  --retry-backoff-seconds 15 ^
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
