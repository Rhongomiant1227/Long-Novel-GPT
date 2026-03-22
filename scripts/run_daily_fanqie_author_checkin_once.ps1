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
$scriptPath = Join-Path $repoRoot 'scripts\fanqie_author_checkin.mjs'

& $node.Source $scriptPath --config $configPath --repair-yesterday @CliArgs
exit $LASTEXITCODE
