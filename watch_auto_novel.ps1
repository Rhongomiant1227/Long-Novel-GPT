param(
    [string]$ProjectDir = "$PSScriptRoot\auto_projects\default_project",
    [string]$BriefFile = "$PSScriptRoot\novel_brief.md",
    [int]$CheckIntervalSeconds = 30,
    [int]$RestartDelaySeconds = 15,
    [int]$StallTimeoutSeconds = 900,
    [switch]$LiveStream
)

$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$logsDir = Join-Path $ProjectDir 'logs'
$statePath = Join-Path $ProjectDir 'state.json'
$runnerPidPath = Join-Path $ProjectDir 'runner.pid'
$supervisorPidPath = Join-Path $ProjectDir 'supervisor.pid'
$supervisorLogPath = Join-Path $logsDir 'supervisor.log'
$stdoutPath = Join-Path $logsDir 'console.out.log'
$stderrPath = Join-Path $logsDir 'console.err.log'
$runnerLogPath = Join-Path $logsDir 'runner.log'

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
Set-Content -Path $supervisorPidPath -Value $PID -Encoding UTF8

function Write-Log([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -Path $supervisorLogPath -Value $line -Encoding UTF8
    Write-Host $line
}

function Read-State {
    if (-not (Test-Path $statePath)) {
        return $null
    }

    try {
        return Get-Content $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        Write-Log ("Failed to read state.json: {0}" -f $_.Exception.Message)
        return $null
    }
}

function Get-StateStatus($State) {
    if ($null -eq $State) {
        return ''
    }
    return [string]$State.status
}

function Get-RunnerProcess {
    if (-not (Test-Path $runnerPidPath)) {
        return $null
    }

    $runnerPid = (Get-Content $runnerPidPath -Raw -Encoding UTF8).Trim()
    if (-not $runnerPid) {
        return $null
    }

    try {
        return Get-Process -Id $runnerPid -ErrorAction Stop
    }
    catch {
        return $null
    }
}

function Get-LastActivityTime($State) {
    $times = @()

    if ($null -ne $State -and $State.updated_at) {
        try {
            $times += [datetime]::Parse([string]$State.updated_at)
        }
        catch {
        }
    }

    foreach ($path in @($runnerLogPath, $stdoutPath, $stderrPath)) {
        if (Test-Path $path) {
            $times += (Get-Item $path).LastWriteTime
        }
    }

    if ($times.Count -eq 0) {
        return $null
    }

    return ($times | Sort-Object -Descending | Select-Object -First 1)
}

function Stop-Runner([string]$Reason) {
    $runner = Get-RunnerProcess
    if ($null -eq $runner) {
        return
    }

    Write-Log ("Stopping runner PID={0}. Reason: {1}" -f $runner.Id, $Reason)
    try {
        Stop-Process -Id $runner.Id -Force -ErrorAction Stop
    }
    catch {
        Write-Log ("Failed to stop runner PID={0}: {1}" -f $runner.Id, $_.Exception.Message)
    }

    Start-Sleep -Seconds 2
}

function Start-Runner {
    if (-not (Test-Path $python)) {
        throw "Virtualenv Python not found: $python"
    }

    $args = @(
        'auto_novel.py',
        '--project-dir', $ProjectDir,
        '--brief-file', $BriefFile,
        '--target-chars', '2000000',
        '--chapter-char-target', '2200',
        '--chapters-per-volume', '30',
        '--chapters-per-batch', '5',
        '--memory-refresh-interval', '5',
        '--main-model', 'sub2api/gpt-5.4',
        '--sub-model', 'sub2api/gpt-5.4',
        '--max-thread-num', '1'
    )

    if ($LiveStream) {
        $args += '--live-stream'
    }

    Write-Log 'Runner is not active. Starting or resuming now.'
    $proc = Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $root -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
    Set-Content -Path $runnerPidPath -Value $proc.Id -Encoding UTF8
    Write-Log ("Runner started. PID={0}" -f $proc.Id)
}

Write-Log ("Supervisor started. Stall timeout={0}s, check interval={1}s." -f $StallTimeoutSeconds, $CheckIntervalSeconds)

while ($true) {
    $state = Read-State
    $status = Get-StateStatus $state

    if ($status -eq 'completed') {
        Write-Log 'Project is marked completed. Supervisor will exit.'
        break
    }

    $runner = Get-RunnerProcess

    if ($runner) {
        $lastActivity = Get-LastActivityTime $state
        if ($lastActivity) {
            $idleSeconds = [int]((Get-Date) - $lastActivity).TotalSeconds
            if ($idleSeconds -ge $StallTimeoutSeconds) {
                Stop-Runner ("No activity for ${idleSeconds}s since {0}" -f $lastActivity.ToString('yyyy-MM-dd HH:mm:ss'))
                $runner = $null
            }
        }

        if ($status -eq 'failed') {
            Stop-Runner 'State file is marked failed while runner is still alive.'
            $runner = $null
        }
    }

    if (-not $runner) {
        Start-Runner
        Start-Sleep -Seconds $RestartDelaySeconds
    }

    Start-Sleep -Seconds $CheckIntervalSeconds
}
