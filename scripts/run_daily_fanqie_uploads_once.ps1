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
$runDir = Join-Path $repoRoot '.run'
New-Item -ItemType Directory -Path $runDir -Force | Out-Null
$lockPath = Join-Path $runDir 'fanqie_daily_uploads.lock'

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

function Acquire-UploadLock {
    param([string]$Path)

    try {
        return [System.IO.File]::Open($Path, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    } catch {
        return $null
    }
}

function Invoke-UploadRun {
    param([string[]]$Arguments)

    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) {
        Write-Output '未在 PATH 中找到 Node.js。'
        return 1
    }

    $configPath = Join-Path $repoRoot 'fanqie_daily_jobs.json'
    $config = Get-Content -Path $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $authorCheckIn = $config.authorCheckIn
    $runCheckInAfterUpload = $false
    if ($authorCheckIn -and $authorCheckIn.enabled -ne $false -and $authorCheckIn.runAfterUpload -eq $true) {
        $runCheckInAfterUpload = $true
    }
    $isDryRun = $false
    if ($Arguments) {
        $isDryRun = $Arguments -contains '--dry-run'
    }

    $scheduler = Join-Path $repoRoot 'scripts\fanqie_daily_scheduler.mjs'
    $checkInRunner = Join-Path $repoRoot 'scripts\run_daily_fanqie_author_checkin_once.ps1'

    & $node.Source $scheduler --config $configPath --once --run-now --retry-until-success @Arguments
    $uploadExitCode = $LASTEXITCODE
    if ($uploadExitCode -ne 0) {
        return $uploadExitCode
    }

    if ($isDryRun) {
        Write-Output '当前为 dry-run，跳过签到修复。'
        return 0
    }

    if (-not $runCheckInAfterUpload) {
        return 0
    }

    if (-not (Test-Path $checkInRunner)) {
        Write-Output "未找到签到修复执行脚本：$checkInRunner"
        return 1
    }

    Write-Output '上传任务已完成，开始执行签到修复。'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $checkInRunner
    $checkInExitCode = $LASTEXITCODE
    if ($checkInExitCode -ne 0) {
        Write-Output ("签到修复执行失败，退出码：{0}" -f $checkInExitCode)
        return $checkInExitCode
    }

    Write-Output '上传任务与签到修复均已完成。'
    return 0
}

$uploadLock = Acquire-UploadLock -Path $lockPath
if (-not $uploadLock) {
    Write-Output '已有番茄上传任务在运行，跳过本次触发。'
    exit 0
}

try {
    $finalExitCode = Invoke-UploadRun -Arguments $CliArgs
} finally {
    $uploadLock.Dispose()
}
exit $finalExitCode
