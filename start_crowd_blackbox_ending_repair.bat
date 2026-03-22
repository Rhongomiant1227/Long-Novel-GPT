@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

set "PROJECT_DIR=%~dp0auto_projects\crowd_blackbox"
set "PYTHON_CMD=.venv\Scripts\python.exe"
set "OUT_LOG=%PROJECT_DIR%\logs\ending_quality_repair_run.log"
set "ERR_LOG=%PROJECT_DIR%\logs\ending_quality_repair_run.err.log"

if not exist "%PYTHON_CMD%" (
  where python >nul 2>nul
  if errorlevel 1 exit /b 1
  set "PYTHON_CMD=python"
)

if "%GPT_API_KEY%"=="" (
  for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "$p = Join-Path $env:USERPROFILE '.codex\\auth.json'; if (Test-Path $p) { $json = Get-Content $p -Raw | ConvertFrom-Json; if ($json.OPENAI_API_KEY) { [Console]::Write($json.OPENAI_API_KEY) } }"`) do set "GPT_API_KEY=%%I"
)

if "%GPT_BASE_URL%"=="" set "GPT_BASE_URL=https://fast.vpsairobot.com/v1"
if /I "%GPT_BASE_URL%"=="https://vpsairobot.com" set "GPT_BASE_URL=https://vpsairobot.com/v1"
if /I "%GPT_BASE_URL%"=="https://fast.vpsairobot.com" set "GPT_BASE_URL=https://fast.vpsairobot.com/v1"
if "%GPT_AVAILABLE_MODELS%"=="" set "GPT_AVAILABLE_MODELS=gpt-5.4"
if "%GPT_MAX_INPUT_TOKENS%"=="" set "GPT_MAX_INPUT_TOKENS=350000"
if "%GPT_MAX_OUTPUT_TOKENS%"=="" set "GPT_MAX_OUTPUT_TOKENS=65536"

"%PYTHON_CMD%" -X utf8 scripts\ending_quality_repair.py ^
  --project-dir auto_projects\crowd_blackbox ^
  --critic-model gpt/gpt-5.4 ^
  --critic-reasoning-effort high ^
  --critic-max-passes 3 ^
  --ending-polish-model gpt/gpt-5.4 ^
  --ending-polish-reasoning-effort xhigh ^
  --max-cycles 3 ^
  1>"%OUT_LOG%" 2>"%ERR_LOG%"

exit /b %ERRORLEVEL%
