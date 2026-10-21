@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "REPO_ROOT=%~dp0."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\monitor_novel_progress.ps1" -RepoRoot "%REPO_ROOT%" %*
