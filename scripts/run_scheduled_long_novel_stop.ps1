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

if ($TaskName) {
    $deleteOutput = & schtasks /Delete /TN $TaskName /F 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Output ("Deleted scheduled task: {0}" -f $TaskName)
    } else {
        Write-Warning ("Failed to delete scheduled task: {0}`n{1}" -f $TaskName, ($deleteOutput -join [Environment]::NewLine))
    }
}

exit $stopExitCode
