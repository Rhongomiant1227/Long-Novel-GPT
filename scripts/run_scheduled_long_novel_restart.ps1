param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectTag,

    [int]$RewriteFromChapter = 0,

    [string]$TaskName = '',

    [int]$GraceSeconds = 20
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$repoRoot = Split-Path -Parent $PSScriptRoot
$projectDir = Join-Path $repoRoot ("auto_projects\" + $ProjectTag)
$logsDir = Join-Path $projectDir 'logs'
$logPath = Join-Path $logsDir 'scheduled_restart.log'

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

function Write-Log {
    param([string]$Message)

    $line = '{0} | [scheduled_restart] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -Path $logPath -Value $line -Encoding UTF8
    Write-Output $line
}

function Resolve-Python {
    $venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
    if (Test-Path $venvPython) {
        return $venvPython
    }
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        return $pythonCmd.Source
    }
    throw 'Python executable not found.'
}

function Invoke-LoggedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [string]$FailureMessage = 'Command failed.'
    )

    $output = & $FilePath @Arguments 2>&1
    foreach ($line in $output) {
        if ($null -ne $line -and "$line".Length -gt 0) {
            Write-Log ([string]$line)
        }
    }
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

try {
    if (-not (Test-Path $projectDir)) {
        throw "Project directory not found: $projectDir"
    }

    Write-Log ("restart requested for project={0}, rewrite_from={1}" -f $ProjectTag, $RewriteFromChapter)

    $stopScript = Join-Path $PSScriptRoot 'stop_long_novel_project.ps1'
    if (Test-Path $stopScript) {
        Invoke-LoggedCommand `
            -FilePath 'powershell.exe' `
            -Arguments @(
                '-NoProfile',
                '-ExecutionPolicy', 'Bypass',
                '-File', $stopScript,
                '-ProjectTag', $ProjectTag,
                '-GraceSeconds', [string]$GraceSeconds
            ) `
            -FailureMessage "Failed to stop project before restart: $ProjectTag"
    }

    if ($RewriteFromChapter -gt 0) {
        $pythonExe = Resolve-Python
        $rewindScript = Join-Path $PSScriptRoot 'rewind_project_from_chapter.py'
        if (-not (Test-Path $rewindScript)) {
            throw "Rewind script not found: $rewindScript"
        }
        Invoke-LoggedCommand `
            -FilePath $pythonExe `
            -Arguments @(
                $rewindScript,
                '--project-dir', $projectDir,
                '--rewrite-from-chapter', [string]$RewriteFromChapter
            ) `
            -FailureMessage "Failed to rewind project before restart: $ProjectTag"
    }

    $startBat = Join-Path $repoRoot ("start_{0}_cli.bat" -f $ProjectTag)
    if (-not (Test-Path $startBat)) {
        throw "Launcher not found: $startBat"
    }

    $proc = Start-Process -FilePath $startBat -WorkingDirectory $repoRoot -PassThru
    Write-Log ("launcher started: pid={0}, file={1}" -f $proc.Id, $startBat)
}
finally {
    if ($TaskName) {
        $deleteOutput = & schtasks /Delete /TN $TaskName /F 2>&1
        foreach ($line in $deleteOutput) {
            if ($null -ne $line -and "$line".Length -gt 0) {
                Write-Log ([string]$line)
            }
        }
    }
}

exit 0
