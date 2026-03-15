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
    Write-Output 'Node.js was not found in PATH.'
    Read-Host 'Press Enter to close' | Out-Null
    exit 1
}

$configPath = Join-Path $repoRoot 'fanqie_daily_jobs.json'
$scheduler = Join-Path $repoRoot 'scripts\fanqie_daily_scheduler.mjs'

Write-Output 'Starting fanqie daily scheduler...'
Write-Output ("Config: {0}" -f $configPath)
Write-Output ''
Write-Output 'The scheduler keeps running and triggers uploads based on the configured local time.'
Write-Output 'Close this window to stop the scheduler.'
Write-Output ''

& $node.Source $scheduler --config $configPath @CliArgs
$exitCode = $LASTEXITCODE

Write-Output ''
if ($exitCode -eq 0) {
    Write-Output 'Scheduler exited normally.'
} else {
    Write-Output ("Scheduler stopped with exit code {0}." -f $exitCode)
}
Read-Host 'Press Enter to close' | Out-Null
exit $exitCode
