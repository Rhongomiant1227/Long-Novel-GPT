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
        return [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch {
        return $null
    }
}

function Invoke-CatchUpRun {
    param([string[]]$Arguments)

    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) {
        Write-Output 'Node.js was not found in PATH.'
        return 1
    }

    $configPath = Join-Path $repoRoot 'fanqie_daily_jobs.json'
    $scheduler = Join-Path $repoRoot 'scripts\fanqie_daily_scheduler.mjs'

    Write-Output 'Start catch-up check after logon. Upload runs only for overdue unfinished jobs.'
    & $node.Source $scheduler --config $configPath --once --retry-until-success --skip-delay @Arguments
    return $LASTEXITCODE
}

$uploadLock = Acquire-UploadLock -Path $lockPath
if (-not $uploadLock) {
    Write-Output 'Another upload process is already running. Skip this logon catch-up run.'
    exit 0
}

try {
    $finalExitCode = Invoke-CatchUpRun -Arguments $CliArgs
} finally {
    $uploadLock.Dispose()
}

exit $finalExitCode
