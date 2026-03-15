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
    exit 1
}

$configPath = Join-Path $repoRoot 'fanqie_daily_jobs.json'
$scheduler = Join-Path $repoRoot 'scripts\fanqie_daily_scheduler.mjs'

& $node.Source $scheduler --config $configPath --once --run-now @CliArgs
exit $LASTEXITCODE
