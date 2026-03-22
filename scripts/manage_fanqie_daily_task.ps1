param(
    [string]$ConfigPath = '',
    [string]$Action = '',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
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
$taskSchedule = $config.taskSchedule
if (-not $taskSchedule) {
    throw "配置文件中未找到 taskSchedule：$resolvedConfig"
}

$taskName = [string]$taskSchedule.taskName
$authorCheckIn = $config.authorCheckIn
$authorCheckInEnabled = $false
$authorCheckInRunsAfterUpload = $false
if ($authorCheckIn) {
    $authorCheckInEnabled = ($authorCheckIn.enabled -ne $false)
    $authorCheckInRunsAfterUpload = ($authorCheckInEnabled -and $authorCheckIn.runAfterUpload -eq $true)
}
$authorTaskName = if ($authorCheckIn) {
    if ($authorCheckIn.taskName) {
        [string]$authorCheckIn.taskName
    } else {
        "$taskName-AuthorCheckIn"
    }
} else {
    ''
}
$runner = Join-Path $repoRoot 'run_daily_fanqie_uploads_once.bat'
$authorRunner = Join-Path $repoRoot 'run_daily_fanqie_author_checkin_once.bat'
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
    param([string]$TaskNameForQuery)

    $output = & cmd.exe /d /c schtasks /Query /TN $TaskNameForQuery /V /FO LIST 2>&1
    if ($LASTEXITCODE -ne 0) {
        return "未找到计划任务：$TaskNameForQuery`n$output"
    }

    $map = Parse-TaskQueryOutput $output
    $summary = @(
        ("任务名称：{0}" -f (Get-FieldValue $map @('TaskName') $TaskNameForQuery)),
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

function Get-ManagedTasks {
    $tasks = @(
        @{
            Key = 'upload'
            Label = '上传任务'
            TaskName = $taskName
            Runner = $runner
            ConfigEnabled = $true
        }
    )

    if ($authorCheckIn -and $authorCheckInEnabled -and -not $authorCheckInRunsAfterUpload) {
        $tasks += @{
            Key = 'checkin'
            Label = '签到修复任务'
            TaskName = $authorTaskName
            Runner = $authorRunner
            ConfigEnabled = $true
        }
    }

    return $tasks
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
    Write-Output "上传执行脚本：$runner"
    if ($authorCheckIn) {
        Write-Output "签到执行脚本：$authorRunner"
    }
    Write-Output "如需修改每周执行日期或每本书的发章数量，请直接编辑上面的配置文件。"

    foreach ($managedTask in @(Get-ManagedTasks)) {
        Write-Section ("当前状态 - {0}" -f $managedTask.Label)
        Write-Output (Get-TaskStatusText -TaskNameForQuery $managedTask.TaskName)
    }
    if ($authorCheckInRunsAfterUpload) {
        Write-Section '当前状态 - 签到修复'
        Write-Output '执行方式：跟随上传任务串行执行'
        Write-Output ("关联上传任务：{0}" -f $taskName)
        Write-Output ("独立计划任务：不注册（原任务名：{0}）" -f $authorTaskName)
        Write-Output '触发条件：上传任务全部成功完成后立即执行'
    }

    Write-Section '配置摘要'
    Write-Output ("任务名称：{0}" -f $taskName)
    Write-Output ("每周执行日：{0}" -f (Format-WeekdayList @($taskSchedule.days)))
    Write-Output ("开始时间：{0}" -f $taskSchedule.startTime)
    Write-Output ("默认随机窗口：{0} 分钟" -f $config.defaults.publishWindowMinutes)
    if ($authorCheckIn) {
        $checkInDays = if ($authorCheckIn.days) { @($authorCheckIn.days) } else { @($taskSchedule.days) }
        $checkInStartTime = if ($authorCheckIn.startTime) { [string]$authorCheckIn.startTime } else { '02:50' }
        $checkInEnabled = if ($authorCheckInEnabled) { '是' } else { '否' }
        Write-Output ("签到修复已启用：{0}" -f $checkInEnabled)
        if ($authorCheckInRunsAfterUpload) {
            Write-Output '签到修复模式：跟随上传任务执行'
            Write-Output '签到修复触发时机：两篇小说上传完成之后立即执行'
            Write-Output ("独立计划任务：不注册（配置中的开始时间 {0} 仅作兼容保留）" -f $checkInStartTime)
        } else {
            Write-Output ("签到修复执行日：{0}" -f (Format-WeekdayList $checkInDays))
            Write-Output ("签到修复开始时间：{0}" -f $checkInStartTime)
        }
    }
    foreach ($job in @($config.jobs)) {
        $jobWindowProp = $job.PSObject.Properties['publishWindowMinutes']
        $window = if ($jobWindowProp -and $null -ne $jobWindowProp.Value) { $jobWindowProp.Value } else { $config.defaults.publishWindowMinutes }
        Write-Output ("- {0} | {1} | 发布时间={2} | 窗口={3}分钟 | AI创作={4} | 发章数={5}" -f $job.id, $job.bookTitle, $job.publishTime, $window, (Format-AIUsage $job $config.defaults), (Format-WeekdayCounts $job $config.defaults))
    }
}

function Change-TaskState {
    param(
        [ValidateSet('ENABLE', 'DISABLE')]
        [string]$Mode
    )

    foreach ($managedTask in @(Get-ManagedTasks)) {
        if (-not $managedTask.ConfigEnabled) {
            continue
        }

        $output = & cmd.exe /d /c schtasks /Change /TN $managedTask.TaskName /$Mode 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw ($output -join [Environment]::NewLine)
        }

        $actionText = if ($Mode -eq 'ENABLE') { '启用' } else { '禁用' }
        Write-Output ("已{0}计划任务：{1}" -f $actionText, $managedTask.TaskName)
    }
}

function Enable-Task {
    Change-TaskState -Mode 'ENABLE'
}

function Disable-Task {
    Change-TaskState -Mode 'DISABLE'
}

function Reinstall-Task {
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer -ConfigPath $resolvedConfig | Out-Host
}

function Run-OnceNow {
    & cmd /c $runner --skip-delay @ExtraArgs | Out-Host
}

function Run-OneChapterNow {
    & cmd /c $runner --skip-delay --chapter-count 1 @ExtraArgs | Out-Host
}

function Run-CheckInNow {
    if (-not $authorCheckIn) {
        throw '当前配置中未定义 authorCheckIn。'
    }
    & cmd /c $authorRunner @ExtraArgs | Out-Host
}

function Invoke-Action {
    param([string]$Name)

    switch ($Name.ToLowerInvariant()) {
        'enable' { Enable-Task; return }
        'disable' { Disable-Task; return }
        'reinstall' { Reinstall-Task; return }
        'run' { Run-OnceNow; return }
        'run-one' { Run-OneChapterNow; return }
        'run-checkin' { Run-CheckInNow; return }
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
        foreach ($managedTask in @(Get-ManagedTasks)) {
            Write-Section ("执行后状态 - {0}" -f $managedTask.Label)
            Write-Output (Get-TaskStatusText -TaskNameForQuery $managedTask.TaskName)
        }
    }
    exit 0
}

while ($true) {
    Show-Status
    Write-Section '可执行操作'
    Write-Output '[1] 启用任务'
    Write-Output '[2] 禁用任务'
    Write-Output '[3] 重新安装或更新任务'
    Write-Output '[4] 立即执行一次上传'
    Write-Output '[5] 立即只发一章'
    Write-Output '[6] 立即执行签到修复'
    Write-Output '[7] 刷新状态'
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
        '6' { Run-CheckInNow; Read-Host '按回车键返回' | Out-Null }
        '7' { }
        'Q' { break }
        default { Write-Host "无效选项：$choice"; Start-Sleep -Seconds 1 }
    }
}
