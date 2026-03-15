param(
    [string]$ConfigPath = '',
    [string]$Action = ''
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
$taskSchedule = $config.taskSchedule
if (-not $taskSchedule) {
    throw "配置文件中未找到 taskSchedule：$resolvedConfig"
}

$taskName = [string]$taskSchedule.taskName
$runner = Join-Path $repoRoot 'run_daily_fanqie_uploads_once.bat'
$installer = Join-Path $repoRoot 'scripts\install_fanqie_daily_task.ps1'

function Write-Section {
    param([string]$Title)
    Write-Output ""
    Write-Output "===== $Title ====="
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

function Get-TaskStatusText {
    $output = & schtasks /Query /TN $taskName /V /FO LIST 2>&1
    if ($LASTEXITCODE -ne 0) {
        return "未找到计划任务：$taskName`n$output"
    }

    $map = Parse-TaskQueryOutput $output
    $summary = @(
        ("任务名称：{0}" -f (Get-FieldValue $map @('TaskName') $taskName)),
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
    return ($summary -join [Environment]::NewLine)
}

function Format-WeekdayCounts {
    param(
        $Job,
        $Defaults
    )

    $map = $Job.chaptersPerRunByWeekday
    if (-not $map -and $Defaults) {
        $map = $Defaults.chaptersPerRunByWeekday
    }
    if ($map) {
        $nameMap = @{
            'mon' = '周一'
            'tue' = '周二'
            'wed' = '周三'
            'thu' = '周四'
            'fri' = '周五'
            'sat' = '周六'
            'sun' = '周日'
        }
        $parts = @()
        foreach ($key in 'mon','tue','wed','thu','fri','sat','sun') {
            $value = $map.$key
            if ($null -ne $value) {
                $parts += ("{0}={1}" -f $nameMap[$key], $value)
            }
        }
        if ($parts.Count -gt 0) {
            return ($parts -join '，')
        }
    }

    if ($null -ne $Job.chaptersPerRun) {
        return "默认=$($Job.chaptersPerRun)"
    }
    return '未设置'
}

function Format-AIUsage {
    param(
        $Job,
        $Defaults
    )

    $rawValue = $null
    $jobProp = $Job.PSObject.Properties['aiUsage']
    if ($jobProp -and $null -ne $jobProp.Value) {
        $rawValue = [string]$jobProp.Value
    }
    if (-not $rawValue -and $Defaults) {
        $defaultProp = $Defaults.PSObject.Properties['aiUsage']
        if ($defaultProp -and $null -ne $defaultProp.Value) {
            $rawValue = [string]$defaultProp.Value
        }
    }

    if (-not $rawValue) {
        $rawValue = ''
    }

    switch ($rawValue.ToLowerInvariant()) {
        'yes' { return '是' }
        'true' { return '是' }
        '1' { return '是' }
        '是' { return '是' }
        default { return '否' }
    }
}

function Show-Status {
    Write-Output "番茄日更计划任务管理器"
    Write-Output "配置文件：$resolvedConfig"
    Write-Output "单次执行脚本：$runner"
    Write-Output "如需修改每周执行日期或每本书的发章数量，请直接编辑上面的配置文件。"

    Write-Section '当前任务状态'
    Write-Output (Get-TaskStatusText)

    Write-Section '配置摘要'
    Write-Output ("任务名称：{0}" -f $taskName)
    Write-Output ("每周执行日：{0}" -f (Format-WeekdayList @($taskSchedule.days)))
    Write-Output ("开始时间：{0}" -f $taskSchedule.startTime)
    Write-Output ("默认随机窗口：{0} 分钟" -f $config.defaults.publishWindowMinutes)
    foreach ($job in @($config.jobs)) {
        $jobWindowProp = $job.PSObject.Properties['publishWindowMinutes']
        $window = if ($jobWindowProp -and $null -ne $jobWindowProp.Value) { $jobWindowProp.Value } else { $config.defaults.publishWindowMinutes }
        Write-Output ("- {0} | {1} | 发布时间={2} | 窗口={3}分钟 | AI创作={4} | 发章数={5}" -f $job.id, $job.bookTitle, $job.publishTime, $window, (Format-AIUsage $job $config.defaults), (Format-WeekdayCounts $job $config.defaults))
    }
}

function Enable-Task {
    $output = & schtasks /Change /TN $taskName /ENABLE 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ($output -join [Environment]::NewLine)
    }
    Write-Output ("已启用计划任务：{0}" -f $taskName)
}

function Disable-Task {
    $output = & schtasks /Change /TN $taskName /DISABLE 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ($output -join [Environment]::NewLine)
    }
    Write-Output ("已禁用计划任务：{0}" -f $taskName)
}

function Reinstall-Task {
    powershell -ExecutionPolicy Bypass -File $installer -ConfigPath $resolvedConfig | Out-Host
}

function Run-OnceNow {
    cmd /c $runner --skip-delay | Out-Host
}

function Run-OneChapterNow {
    cmd /c $runner --skip-delay --chapter-count 1 | Out-Host
}

function Invoke-Action {
    param([string]$Name)
    switch ($Name.ToLowerInvariant()) {
        'enable' { Enable-Task; return }
        'disable' { Disable-Task; return }
        'reinstall' { Reinstall-Task; return }
        'run' { Run-OnceNow; return }
        'run-one' { Run-OneChapterNow; return }
        'status' { Show-Status; return }
        default { throw "未知动作：$Name" }
    }
}

if ($Action) {
    $normalizedAction = $Action.ToLowerInvariant()
    if ($normalizedAction -ne 'status') {
        Show-Status
    }
    Invoke-Action $Action
    if ($normalizedAction -ne 'status') {
        Write-Section '执行后状态'
        Write-Output (Get-TaskStatusText)
    }
    exit 0
}

while ($true) {
    Show-Status
    Write-Section '可执行操作'
    Write-Output '[1] 启用任务'
    Write-Output '[2] 禁用任务'
    Write-Output '[3] 重新安装或更新任务'
    Write-Output '[4] 立即执行一次'
    Write-Output '[5] 立即只发一章'
    Write-Output '[6] 刷新状态'
    Write-Output '[Q] 退出'
    try {
        $choice = Read-Host '请选择'
    } catch {
        break
    }
    if ($null -eq $choice) {
        break
    }
    switch ($choice.ToUpperInvariant()) {
        '1' { Enable-Task; Start-Sleep -Seconds 1 }
        '2' { Disable-Task; Start-Sleep -Seconds 1 }
        '3' { Reinstall-Task; Start-Sleep -Seconds 1 }
        '4' { Run-OnceNow; Read-Host '按回车键返回' | Out-Null }
        '5' { Run-OneChapterNow; Read-Host '按回车键返回' | Out-Null }
        '6' { }
        'Q' { break }
        default { Write-Host "无效选项：$choice"; Start-Sleep -Seconds 1 }
    }
}
