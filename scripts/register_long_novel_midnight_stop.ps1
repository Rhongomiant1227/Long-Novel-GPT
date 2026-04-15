param(
    [Parameter(Mandatory = $true)]
    [string[]]$ProjectTags,

    [datetime]$StopAt = [datetime]::MinValue,

    [int]$GraceSeconds = 20,

    [switch]$ForceRegistration
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$runnerScript = Join-Path $PSScriptRoot 'run_scheduled_long_novel_stop.ps1'
if (-not (Test-Path $runnerScript)) {
    throw "Scheduled stop runner not found: $runnerScript"
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$disableFlagPath = Join-Path $repoRoot '.run\disable_long_novel_auto_stop.flag'
if ((Test-Path $disableFlagPath) -and -not $ForceRegistration) {
    throw ("Scheduled long-novel auto stop is globally disabled by flag: {0}" -f $disableFlagPath)
}

$now = Get-Date
$stopTime = if ($StopAt -eq [datetime]::MinValue) {
    $now.Date.AddDays(1)
} else {
    $StopAt
}

if ($stopTime -le $now) {
    throw ("Stop time must be later than current time. Current={0}, Stop={1}" -f $now.ToString('yyyy-MM-dd HH:mm:ss'), $stopTime.ToString('yyyy-MM-dd HH:mm:ss'))
}

$taskDate = $stopTime.ToString('yyyy/MM/dd')
$taskClock = $stopTime.ToString('HH:mm')

foreach ($projectTag in $ProjectTags) {
    $projectDir = Join-Path $repoRoot ("auto_projects\" + $projectTag)
    if (-not (Test-Path $projectDir)) {
        throw "Project directory not found: $projectDir"
    }

    $taskName = 'LongNovel_Stop_{0}_{1}' -f $projectTag, $stopTime.ToString('yyyyMMdd_HHmm')
    $taskCommand = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}" -ProjectTag "{1}" -TaskName "{2}" -GraceSeconds {3}' -f $runnerScript, $projectTag, $taskName, $GraceSeconds

    $createArgs = @(
        '/Create',
        '/SC', 'ONCE',
        '/SD', $taskDate,
        '/ST', $taskClock,
        '/TN', $taskName,
        '/TR', $taskCommand,
        '/F'
    )

    $createOutput = & schtasks @createArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ($createOutput -join [Environment]::NewLine)
    }

    Write-Output ("Created stop task: {0}" -f $taskName)
    Write-Output ("Project: {0}" -f $projectTag)
    Write-Output ("Stop time: {0}" -f $stopTime.ToString('yyyy-MM-dd HH:mm:ss'))
    Write-Output ("Command: {0}" -f $taskCommand)
    Write-Output ""
}
