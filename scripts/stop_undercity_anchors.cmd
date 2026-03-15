@echo off
setlocal
set "TASK_NAME=%~1"
set "SCRIPT_DIR=%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%stop_long_novel_project.ps1" -ProjectTag undercity_anchors -GraceSeconds 20

if not "%TASK_NAME%"=="" (
  schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>nul
  exit /b 0
)

schtasks /Delete /TN "LongNovel_Stop_undercity_anchors_20260315_0000" /F >nul 2>nul
