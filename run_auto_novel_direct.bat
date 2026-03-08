@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Python virtualenv not found.
  echo Please run `run.bat` once first, or create `.venv` manually.
  echo Press any key to close this window.
  pause >nul
  exit /b 1
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

echo Starting automatic long-novel runner...
echo Project: %~dp0auto_projects\default_project
echo.

".venv\Scripts\python.exe" auto_novel.py ^
  --project-dir "%~dp0auto_projects\default_project" ^
  --brief-file "%~dp0novel_brief.md" ^
  --target-chars 2000000 ^
  --chapter-char-target 2200 ^
  --chapters-per-volume 30 ^
  --chapters-per-batch 5 ^
  --memory-refresh-interval 5 ^
  --main-model gpt/gpt-5.4 ^
  --sub-model gpt/gpt-5.4 ^
  --max-thread-num 1 ^
  --live-stream %*

set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
  echo Automatic long-novel runner finished successfully.
) else (
  echo Automatic long-novel runner stopped with exit code %EXIT_CODE%.
)
echo Press any key to close this window.
pause >nul
exit /b %EXIT_CODE%
