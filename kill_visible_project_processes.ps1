param(
    [Parameter(Mandatory = $true)]
    [string]$Project
)

$ErrorActionPreference = 'SilentlyContinue'
$repo = 'D:\Long-Novel-GPT'
$needle = $Project.ToLowerInvariant()

$processes = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and
    $_.Name -notmatch '^(powershell|pwsh)(\.exe)?$' -and
    $_.CommandLine -and $_.CommandLine.ToLowerInvariant().Contains($needle) -and (
        $_.CommandLine -match 'launch_visible_project\.bat' -or
        $_.CommandLine -match 'watch_auto_novel_visible.py' -or
        $_.CommandLine -match 'auto_novel.py' -or
        $_.CommandLine -match 'start_.*_cli\.bat'
    )
})

foreach ($proc in $processes) {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
}

$lockPath = Join-Path $repo ("auto_projects\{0}\logs\watchdog.instance.json" -f $Project)
if (Test-Path $lockPath) {
    Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
}
