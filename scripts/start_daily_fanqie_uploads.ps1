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
    Read-Host '按回车键关闭窗口' | Out-Null
    exit 1
}

$configPath = Join-Path $repoRoot 'fanqie_daily_jobs.json'
$scheduler = Join-Path $repoRoot 'scripts\fanqie_daily_scheduler.mjs'

Write-Output '正在启动番茄日更调度器...'
Write-Output ("配置文件：{0}" -f $configPath)
Write-Output ''
Write-Output '调度器会持续运行，并按配置的本地时间触发上传。'
Write-Output '关闭此窗口可停止调度器。'
Write-Output ''

& $node.Source $scheduler --config $configPath @CliArgs
$exitCode = $LASTEXITCODE

Write-Output ''
if ($exitCode -eq 0) {
    Write-Output '日更调度器已正常结束。'
} else {
    Write-Output ("日更调度器已结束，退出码为 {0}。" -f $exitCode)
}
Read-Host '按回车键关闭窗口' | Out-Null
exit $exitCode
