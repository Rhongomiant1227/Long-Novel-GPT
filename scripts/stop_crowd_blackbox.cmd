@echo off
setlocal
set "SCRIPT_DIR=%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%stop_long_novel_project.ps1" -ProjectTag crowd_blackbox -GraceSeconds 20
schtasks /Delete /TN "LongNovel_Stop_crowd_blackbox_20260311_0000" /F >nul 2>nul
