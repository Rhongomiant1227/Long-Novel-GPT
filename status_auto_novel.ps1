param(
    [string]$ProjectDir = "$PSScriptRoot\auto_projects\default_project"
)

$ErrorActionPreference = 'Stop'

$statePath = Join-Path $ProjectDir 'state.json'
$logPath = Join-Path $ProjectDir 'logs\runner.log'
$supervisorLogPath = Join-Path $ProjectDir 'logs\supervisor.log'
$runnerPidPath = Join-Path $ProjectDir 'runner.pid'
$supervisorPidPath = Join-Path $ProjectDir 'supervisor.pid'
$runnerHeartbeatPath = Join-Path $ProjectDir 'logs\runner_heartbeat.json'

if (-not (Test-Path $statePath)) {
    Write-Host "State file not found: $statePath"
    exit 1
}

$state = Get-Content $statePath -Raw -Encoding UTF8 | ConvertFrom-Json

function Show-ProcessStatus([string]$Label, [string]$PidPath) {
    if (-not (Test-Path $PidPath)) {
        Write-Host ("{0,-22}: not recorded" -f $Label)
        return
    }

    $pidText = (Get-Content $PidPath -Raw -Encoding UTF8).Trim()
    if (-not $pidText) {
        Write-Host ("{0,-22}: empty" -f $Label)
        return
    }

    try {
        $proc = Get-Process -Id $pidText -ErrorAction Stop
        Write-Host ("{0,-22}: PID {1} running since {2}" -f $Label, $proc.Id, $proc.StartTime.ToString('yyyy-MM-dd HH:mm:ss'))
    }
    catch {
        Write-Host ("{0,-22}: PID {1} not running" -f $Label, $pidText)
    }
}

Write-Host '--- State ---'
Write-Host ("status                : {0}" -f $state.status)
Write-Host ("generated_chapters    : {0}" -f $state.generated_chapters)
Write-Host ("generated_chars       : {0}" -f $state.generated_chars)
if ($state.target_chars) { Write-Host ("target_chars          : {0}" -f $state.target_chars) }
if ($state.min_target_chars) { Write-Host ("min_target_chars      : {0}" -f $state.min_target_chars) }
if ($state.force_finish_chars) { Write-Host ("force_finish_chars    : {0}" -f $state.force_finish_chars) }
if ($state.max_target_chars) { Write-Host ("absolute_max_chars    : {0}" -f $state.max_target_chars) }
Write-Host ("next_chapter_number   : {0}" -f $state.next_chapter_number)
Write-Host ("pending_chapters      : {0}" -f $state.pending_chapters.Count)
Write-Host ("current_volume        : {0}" -f $state.current_volume)
Write-Host ("updated_at            : {0}" -f $state.updated_at)
if ($state.current_stage) { Write-Host ("current_stage         : {0}" -f $state.current_stage) }
if ($state.stage_started_at) { Write-Host ("stage_started_at      : {0}" -f $state.stage_started_at) }
if ($state.last_stage_heartbeat_at) { Write-Host ("stage_heartbeat_at    : {0}" -f $state.last_stage_heartbeat_at) }
if ($state.last_error) {
    Write-Host ("last_error            : {0}" -f $state.last_error)
}
if (Test-Path $runnerHeartbeatPath) {
    try {
        $runnerHeartbeat = Get-Content $runnerHeartbeatPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($runnerHeartbeat.at) { Write-Host ("runner_heartbeat_at  : {0}" -f $runnerHeartbeat.at) }
        if ($runnerHeartbeat.current_stage) { Write-Host ("runner_heartbeat_stage: {0}" -f $runnerHeartbeat.current_stage) }
    }
    catch { }
}

Write-Host ''
Write-Host '--- Processes ---'
Show-ProcessStatus 'runner' $runnerPidPath
Show-ProcessStatus 'supervisor' $supervisorPidPath

if (Test-Path $logPath) {
    $runnerLog = Get-Item $logPath
    Write-Host ''
    Write-Host '--- Runner Log ---'
    Write-Host ("last_write            : {0}" -f $runnerLog.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))
    Write-Host ("size_bytes            : {0}" -f $runnerLog.Length)
    Get-Content $logPath -Tail 40 -Encoding UTF8
}

if (Test-Path $supervisorLogPath) {
    $supervisorLog = Get-Item $supervisorLogPath
    Write-Host ''
    Write-Host '--- Supervisor Log ---'
    Write-Host ("last_write            : {0}" -f $supervisorLog.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))
    Write-Host ("size_bytes            : {0}" -f $supervisorLog.Length)
    Get-Content $supervisorLogPath -Tail 20 -Encoding UTF8
}
