param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectTag,

    [int]$GraceSeconds = 20
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$projectDir = Join-Path $repoRoot ("auto_projects\" + $ProjectTag)
$logsDir = Join-Path $projectDir 'logs'
$logPath = Join-Path $logsDir 'scheduled_stop.log'

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

function Write-Log {
    param([string]$Message)

    $line = '{0} | [scheduled_stop] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -Path $logPath -Value $line -Encoding UTF8
    Write-Output $line
}

function Get-ProjectProcesses {
    $batName = 'start_{0}_cli\.bat' -f [regex]::Escape($ProjectTag)

    Get-CimInstance Win32_Process |
        Where-Object {
            $_.ProcessId -ne $PID -and
            $_.Name -in @('cmd.exe', 'python.exe') -and
            $_.CommandLine -and
            (
                $_.CommandLine -match [regex]::Escape($ProjectTag) -or
                $_.CommandLine -match $batName
            )
        }
}

Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class ConsoleSignal {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool FreeConsole();

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool AttachConsole(uint dwProcessId);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetConsoleCtrlHandler(IntPtr handler, bool add);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GenerateConsoleCtrlEvent(uint ctrlEvent, uint processGroupId);
}
"@

function Send-CtrlBreak {
    param([int]$ConsolePid)

    [void][ConsoleSignal]::FreeConsole()
    if (-not [ConsoleSignal]::AttachConsole([uint32]$ConsolePid)) {
        return $false
    }

    [void][ConsoleSignal]::SetConsoleCtrlHandler([IntPtr]::Zero, $true)
    $sent = [ConsoleSignal]::GenerateConsoleCtrlEvent(1, 0)
    Start-Sleep -Seconds 1
    [void][ConsoleSignal]::FreeConsole()
    [void][ConsoleSignal]::SetConsoleCtrlHandler([IntPtr]::Zero, $false)
    return $sent
}

Write-Log ("stop requested for project={0}, grace={1}s" -f $ProjectTag, $GraceSeconds)

$initial = @(Get-ProjectProcesses)
if (-not $initial) {
    Write-Log 'no matching project processes found; nothing to stop'
    exit 0
}

$consoleTarget = $initial | Where-Object { $_.Name -eq 'cmd.exe' } | Select-Object -First 1
if (-not $consoleTarget) {
    $consoleTarget = $initial | Where-Object { $_.Name -eq 'python.exe' } | Select-Object -First 1
}

if ($consoleTarget) {
    $signalSent = Send-CtrlBreak -ConsolePid $consoleTarget.ProcessId
    Write-Log ("sent CTRL_BREAK to pid={0}, name={1}, ok={2}" -f $consoleTarget.ProcessId, $consoleTarget.Name, $signalSent)
} else {
    Write-Log 'no console target found; skipping CTRL_BREAK stage'
}

Start-Sleep -Seconds $GraceSeconds

$remaining = @(Get-ProjectProcesses)
if (-not $remaining) {
    Write-Log 'project stopped gracefully within grace period'
    exit 0
}

$remainingIds = $remaining | Select-Object -ExpandProperty ProcessId -Unique
Write-Log ("force stopping remaining pids: {0}" -f (($remainingIds | Sort-Object) -join ', '))
foreach ($pidValue in $remainingIds) {
    Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 1
$afterForce = @(Get-ProjectProcesses)
if ($afterForce) {
    Write-Log ("warning: processes still remain after force stop: {0}" -f (($afterForce | Select-Object -ExpandProperty ProcessId -Unique | Sort-Object) -join ', '))
    exit 1
}

Write-Log 'project force-stopped successfully'
exit 0
