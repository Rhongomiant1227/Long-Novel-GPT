param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) {
    Write-Output '未在 PATH 中找到 Node.js。'
    exit 1
}

$configPath = Join-Path $repoRoot 'fanqie_daily_jobs.json'
$config = Get-Content -Path $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
$authorCheckIn = $config.authorCheckIn
$runCheckInAfterUpload = $false
if ($authorCheckIn -and $authorCheckIn.enabled -ne $false -and $authorCheckIn.runAfterUpload -eq $true) {
    $runCheckInAfterUpload = $true
}
$isDryRun = $false
if ($CliArgs) {
    $isDryRun = $CliArgs -contains '--dry-run'
}

$scheduler = Join-Path $repoRoot 'scripts\fanqie_daily_scheduler.mjs'
$checkInRunner = Join-Path $repoRoot 'scripts\run_daily_fanqie_author_checkin_once.ps1'

& $node.Source $scheduler --config $configPath --once --run-now --retry-until-success @CliArgs
$uploadExitCode = $LASTEXITCODE
if ($uploadExitCode -ne 0) {
    exit $uploadExitCode
}

if ($isDryRun) {
    Write-Output '当前为 dry-run，跳过签到修复。'
    exit 0
}

if (-not $runCheckInAfterUpload) {
    exit 0
}

if (-not (Test-Path $checkInRunner)) {
    Write-Output "未找到签到修复执行脚本：$checkInRunner"
    exit 1
}

Write-Output '上传任务已完成，开始执行签到修复。'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $checkInRunner
$checkInExitCode = $LASTEXITCODE
if ($checkInExitCode -ne 0) {
    Write-Output ("签到修复执行失败，退出码：{0}" -f $checkInExitCode)
    exit $checkInExitCode
}

Write-Output '上传任务与签到修复均已完成。'
exit 0
