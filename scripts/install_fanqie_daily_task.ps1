param(
    [string]$ConfigPath = '',
    [string]$TaskName = '',
    [string]$StartTime = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$repoRoot = Split-Path -Parent $PSScriptRoot
$resolvedConfig = if ($ConfigPath) { $ConfigPath } else { Join-Path $repoRoot 'fanqie_daily_jobs.json' }
$resolvedConfig = (Resolve-Path $resolvedConfig).Path
$config = Get-Content -Path $resolvedConfig -Raw -Encoding UTF8 | ConvertFrom-Json

$schedule = $config.taskSchedule
if (-not $schedule) {
    throw "配置文件中未找到 taskSchedule：$resolvedConfig"
}

$taskNameValue = if ($TaskName) { $TaskName } else { [string]$schedule.taskName }
$startTimeValue = if ($StartTime) { $StartTime } else { [string]$schedule.startTime }
$days = @($schedule.days)
if (-not $days.Count) {
    throw "配置文件中的 taskSchedule.days 为空：$resolvedConfig"
}
$daysValue = ($days | ForEach-Object { $_.ToString().ToUpperInvariant() }) -join ','
$authorCheckIn = $config.authorCheckIn
$authorCheckInEnabled = $false
$authorCheckInRunsAfterUpload = $false
if ($authorCheckIn) {
    $authorCheckInEnabled = ($authorCheckIn.enabled -ne $false)
    $authorCheckInRunsAfterUpload = ($authorCheckInEnabled -and $authorCheckIn.runAfterUpload -eq $true)
}

$runner = Join-Path $repoRoot 'run_daily_fanqie_uploads_once.bat'
if (-not (Test-Path $runner)) {
    throw "未找到单次执行脚本：$runner"
}
$authorRunner = Join-Path $repoRoot 'run_daily_fanqie_author_checkin_once.bat'

function Parse-TaskQueryOutput {
    param([string[]]$Lines)

    $map = @{}
    foreach ($line in $Lines) {
        if ($line -match '^\s*([^:]+):\s*(.*)$') {
            $key = $matches[1].Trim()
            $value = $matches[2].Trim()
            if (-not $map.ContainsKey($key)) {
                $map[$key] = $value
            }
        }
    }
    return $map
}

function Get-FieldValue {
    param(
        [hashtable]$Map,
        [string[]]$Names,
        [string]$Default = ''
    )

    foreach ($name in $Names) {
        if ($Map.ContainsKey($name)) {
            $value = [string]$Map[$name]
            if ($value) {
                return $value
            }
        }
    }
    return $Default
}

function Translate-TaskValue {
    param([string]$Value)

    switch ($Value) {
        'Ready' { return '就绪' }
        'Running' { return '运行中' }
        'Disabled' { return '已禁用' }
        'Enabled' { return '已启用' }
        'Queued' { return '已排队' }
        'Could not start' { return '无法启动' }
        'Interactive only' { return '仅交互式运行' }
        'Background only' { return '仅后台运行' }
        'Weekly' { return '每周' }
        'Daily' { return '每天' }
        'Every day of the week' { return '每周每天' }
        'N/A' { return '未提供' }
        default { return $Value }
    }
}

function Format-WeekdayList {
    param([string[]]$Days)

    $nameMap = @{
        'MON' = '周一'
        'TUE' = '周二'
        'WED' = '周三'
        'THU' = '周四'
        'FRI' = '周五'
        'SAT' = '周六'
        'SUN' = '周日'
    }

    $translated = foreach ($day in $Days) {
        $upper = $day.ToUpperInvariant()
        if ($nameMap.ContainsKey($upper)) { $nameMap[$upper] } else { $day }
    }
    return ($translated -join '、')
}

function Format-TaskSummary {
    param(
        [string]$TaskNameForDisplay,
        [string[]]$QueryOutput
    )

    $map = Parse-TaskQueryOutput $QueryOutput
    return @(
        ("任务名称：{0}" -f (Get-FieldValue $map @('TaskName') $TaskNameForDisplay)),
        ("下次运行时间：{0}" -f (Translate-TaskValue (Get-FieldValue $map @('Next Run Time') '未提供'))),
        ("当前状态：{0}" -f (Translate-TaskValue (Get-FieldValue $map @('Status') '未提供'))),
        ("启用状态：{0}" -f (Translate-TaskValue (Get-FieldValue $map @('Scheduled Task State') '未提供'))),
        ("登录方式：{0}" -f (Translate-TaskValue (Get-FieldValue $map @('Logon Mode') '未提供'))),
        ("上次运行时间：{0}" -f (Get-FieldValue $map @('Last Run Time') '未提供')),
        ("上次结果：{0}" -f (Get-FieldValue $map @('Last Result') '未提供')),
        ("执行命令：{0}" -f (Get-FieldValue $map @('Task To Run') '未提供')),
        ("运行用户：{0}" -f (Get-FieldValue $map @('Run As User') '未提供')),
        ("计划类型：{0}" -f (Translate-TaskValue (Get-FieldValue $map @('Schedule Type') '未提供'))),
        ("开始时间：{0}" -f (Get-FieldValue $map @('Start Time') '未提供')),
        ("执行日：{0}" -f (Translate-TaskValue (Get-FieldValue $map @('Days') '未提供'))),
        ("作者：{0}" -f (Get-FieldValue $map @('Author') '未提供'))
    )
}

function Normalize-Days {
    param(
        [object]$RawDays,
        [string[]]$FallbackDays
    )

    $resolved = @($RawDays)
    if (-not $resolved.Count) {
        $resolved = @($FallbackDays)
    }
    if (-not $resolved.Count) {
        throw '计划任务执行日不能为空。'
    }
    return @($resolved | ForEach-Object { $_.ToString().ToUpperInvariant() })
}

function Register-WeeklyTask {
    param(
        [string]$TaskNameForCreate,
        [string]$StartTimeForCreate,
        [string[]]$DaysForCreate,
        [string]$RunnerPath
    )

    $createArgs = @(
        '/Create',
        '/SC', 'WEEKLY',
        '/D', ($DaysForCreate -join ','),
        '/ST', $StartTimeForCreate,
        '/TN', $TaskNameForCreate,
        '/TR', $RunnerPath,
        '/F'
    )

    $queryArgs = @(
        '/Query',
        '/TN', $TaskNameForCreate,
        '/V',
        '/FO', 'LIST'
    )

    $createOutput = & schtasks @createArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ($createOutput -join [Environment]::NewLine)
    }

    $queryOutput = & schtasks @queryArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ($queryOutput -join [Environment]::NewLine)
    }

    return $queryOutput
}

function Remove-TaskIfExists {
    param([string]$TaskNameForDelete)

    try {
        $queryOutput = & cmd.exe /d /c schtasks /Query /TN $TaskNameForDelete /FO LIST 2>$null
    } catch {
        return $false
    }
    if ($LASTEXITCODE -ne 0) {
        return $false
    }

    $deleteOutput = & cmd.exe /d /c schtasks /Delete /TN $TaskNameForDelete /F 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ($deleteOutput -join [Environment]::NewLine)
    }
    return $true
}

$queryOutput = Register-WeeklyTask -TaskNameForCreate $taskNameValue -StartTimeForCreate $startTimeValue -DaysForCreate (Normalize-Days -RawDays $days -FallbackDays @()) -RunnerPath $runner

$authorTaskSummary = $null
$authorTaskNameValue = if ($authorCheckIn) {
    if ($authorCheckIn.taskName) {
        [string]$authorCheckIn.taskName
    } else {
        "$taskNameValue-AuthorCheckIn"
    }
} else {
    ''
}
$authorTaskDays = @()
$authorStartTimeValue = ''
if ($authorCheckInEnabled -and -not $authorCheckInRunsAfterUpload) {
    if (-not (Test-Path $authorRunner)) {
        throw "未找到签到修复执行脚本：$authorRunner"
    }

    $authorStartTimeValue = if ($authorCheckIn.startTime) { [string]$authorCheckIn.startTime } else { '02:50' }
    $authorTaskDays = Normalize-Days -RawDays $authorCheckIn.days -FallbackDays $days
    $authorQueryOutput = Register-WeeklyTask -TaskNameForCreate $authorTaskNameValue -StartTimeForCreate $authorStartTimeValue -DaysForCreate $authorTaskDays -RunnerPath $authorRunner
    $authorTaskSummary = Format-TaskSummary -TaskNameForDisplay $authorTaskNameValue -QueryOutput $authorQueryOutput
} elseif ($authorCheckIn) {
    if (Remove-TaskIfExists -TaskNameForDelete $authorTaskNameValue) {
        if ($authorCheckInRunsAfterUpload) {
            $authorTaskSummary = @("签到修复已改为跟随上传任务执行，已删除独立计划任务：$authorTaskNameValue")
        } else {
            $authorTaskSummary = @("签到修复任务已按配置删除：$authorTaskNameValue")
        }
    } elseif ($authorCheckInRunsAfterUpload) {
        $authorTaskSummary = @("签到修复已改为跟随上传任务执行，不会单独注册计划任务：$authorTaskNameValue")
    }
}

Write-Output "计划任务创建或更新命令已成功执行。"
Write-Output ("计划任务已创建或更新：{0}" -f $taskNameValue)
Write-Output ("配置文件：{0}" -f $resolvedConfig)
Write-Output ("执行脚本：{0}" -f $runner)
Write-Output ("每周执行日：{0}" -f (Format-WeekdayList $days))
Write-Output ("开始时间：{0}" -f $startTimeValue)
if ($authorCheckInRunsAfterUpload) {
    Write-Output "签到修复模式：跟随上传任务串行执行"
    Write-Output "签到修复触发时机：上传任务成功完成后立即执行"
}
Write-Output ""
Write-Output "当前任务状态："
Write-Output ((Format-TaskSummary -TaskNameForDisplay $taskNameValue -QueryOutput $queryOutput) -join [Environment]::NewLine)
if ($authorTaskSummary) {
    Write-Output ""
    Write-Output "签到修复任务状态："
    Write-Output ($authorTaskSummary -join [Environment]::NewLine)
}
