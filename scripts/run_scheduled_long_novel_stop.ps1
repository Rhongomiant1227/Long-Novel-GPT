param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectTag,

    [string]$TaskName = '',

    [int]$GraceSeconds = 20
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$repoRoot = Split-Path -Parent $PSScriptRoot
$disableFlagPath = Join-Path $repoRoot '.run\disable_long_novel_auto_stop.flag'

function Remove-ScheduledTaskIfRequested {
    param([string]$TaskNameToDelete)

    if (-not $TaskNameToDelete) {
        return
    }

    $deleteOutput = & schtasks /Delete /TN $TaskNameToDelete /F 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Output ("Deleted scheduled task: {0}" -f $TaskNameToDelete)
    } else {
        Write-Warning ("Failed to delete scheduled task: {0}`n{1}" -f $TaskNameToDelete, ($deleteOutput -join [Environment]::NewLine))
    }
}

if (Test-Path $disableFlagPath) {
    Write-Output ("Scheduled stop bypassed for project {0}: global disable flag present at {1}" -f $ProjectTag, $disableFlagPath)
    Remove-ScheduledTaskIfRequested -TaskNameToDelete $TaskName
    exit 0
}

$stopScript = Join-Path $PSScriptRoot 'stop_long_novel_project.ps1'
if (-not (Test-Path $stopScript)) {
    throw "Stop script not found: $stopScript"
}

$stopArgs = @(
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-File', $stopScript,
    '-ProjectTag', $ProjectTag,
    '-GraceSeconds', [string]$GraceSeconds
)

& powershell.exe @stopArgs
$stopExitCode = $LASTEXITCODE
if ($null -eq $stopExitCode) {
    $stopExitCode = 0
}

Remove-ScheduledTaskIfRequested -TaskNameToDelete $TaskName

exit $stopExitCode
