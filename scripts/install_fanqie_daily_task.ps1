param(
    [string]$ConfigPath = '',
    [string]$TaskName = '',
    [string]$StartTime = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
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

$runner = Join-Path $repoRoot 'run_daily_fanqie_uploads_once.bat'
if (-not (Test-Path $runner)) {
    throw "未找到单次执行脚本：$runner"
}

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

$createArgs = @(
    '/Create',
    '/SC', 'WEEKLY',
    '/D', $daysValue,
    '/ST', $startTimeValue,
    '/TN', $taskNameValue,
    '/TR', $runner,
    '/F'
)

$queryArgs = @(
    '/Query',
    '/TN', $taskNameValue,
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

Write-Output "计划任务创建或更新命令已成功执行。"
Write-Output ("计划任务已创建或更新：{0}" -f $taskNameValue)
Write-Output ("配置文件：{0}" -f $resolvedConfig)
Write-Output ("执行脚本：{0}" -f $runner)
Write-Output ("每周执行日：{0}" -f (Format-WeekdayList $days))
Write-Output ("开始时间：{0}" -f $startTimeValue)
Write-Output ""
Write-Output "当前任务状态："
Write-Output ((Format-TaskSummary -TaskNameForDisplay $taskNameValue -QueryOutput $queryOutput) -join [Environment]::NewLine)
