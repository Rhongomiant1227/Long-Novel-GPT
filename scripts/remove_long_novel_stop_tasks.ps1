param(
    [Parameter(Mandatory = $true)]
    [string[]]$ProjectTags
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Get-StopTasksForProject {
    param([Parameter(Mandatory = $true)][string]$ProjectTag)

    $patterns = @(
        ('LongNovel_Stop_{0}' -f $ProjectTag),
        ('LongNovel_Stop_{0}_*' -f $ProjectTag)
    )

    $tasks = @()
    foreach ($pattern in $patterns) {
        $tasks += @(Get-ScheduledTask -TaskName $pattern -ErrorAction SilentlyContinue)
    }

    return @($tasks | Sort-Object TaskPath, TaskName -Unique)
}

foreach ($projectTag in $ProjectTags) {
    $tasks = Get-StopTasksForProject -ProjectTag $projectTag
    foreach ($task in $tasks) {
        Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Confirm:$false | Out-Null
        Write-Output ('Deleted scheduled task: {0}{1}' -f $task.TaskPath, $task.TaskName)
    }
}
