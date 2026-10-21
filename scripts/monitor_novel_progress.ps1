param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [int]$RefreshSeconds = 5,
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$RepoRoot = (Resolve-Path $RepoRoot).Path

$script:LogCache = @{}
$script:History = @{}
$script:ApiEndpointCache = @{}
$script:ApiChainOrderCache = $null

function Get-RunningProjects {
    $running = @{}
    Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and $_.CommandLine -match 'watch_auto_novel_visible\.py'
    } | ForEach-Object {
        if ($_.CommandLine -match 'auto_projects\\([^" ]+)') {
            $running[$matches[1]] = $true
        }
    }
    return $running
}

function Get-TotalOutputChars {
    param(
        [string]$ProjectName,
        [string]$ProjectDir
    )

    $logPath = Join-Path $ProjectDir 'logs\events.log'
    if (-not (Test-Path $logPath)) {
        return 0L
    }

    $item = Get-Item $logPath
    $cached = $script:LogCache[$ProjectName]
    if ($cached -and $cached.Length -eq $item.Length -and $cached.LastWriteUtc -eq $item.LastWriteTimeUtc) {
        return [int64]$cached.TotalChars
    }

    [int64]$total = 0
    foreach ($line in Get-Content $logPath -Encoding UTF8 -ErrorAction SilentlyContinue) {
        if ($line -match '输出\s*(\d+)\s*字') {
            $total += [int64]$matches[1]
        }
    }

    $script:LogCache[$ProjectName] = @{
        Length = $item.Length
        LastWriteUtc = $item.LastWriteTimeUtc
        TotalChars = $total
    }
    return $total
}

function Get-ProjectApiEndpointFromLog {
    param(
        [string]$ProjectName,
        [string]$ProjectDir
    )

    $logPath = Join-Path $ProjectDir 'logs\events.log'
    if (-not (Test-Path $logPath)) {
        return '-'
    }

    $item = Get-Item $logPath
    $cached = $script:ApiEndpointCache[$ProjectName]
    if ($cached -and $cached.Length -eq $item.Length -and $cached.LastWriteUtc -eq $item.LastWriteTimeUtc) {
        return [string]$cached.Endpoint
    }

    $recentLines = @()
    try {
        $recentLines = Get-Content -LiteralPath $logPath -Encoding UTF8 -Tail 256 -ErrorAction SilentlyContinue
    } catch {
        $recentLines = @()
    }

    $endpoint = if ($cached) { [string]$cached.Endpoint } else { '-' }
    for ($idx = $recentLines.Count - 1; $idx -ge 0; $idx--) {
        $line = [string]$recentLines[$idx]
        if ($line -match 'API.*?(https?://\S+)') {
            $rawValue = [string]$matches[1]
            $endpoint = Format-ApiEndpointDisplay -Value $rawValue
            break
        }
    }

    $script:ApiEndpointCache[$ProjectName] = @{
        Length = $item.Length
        LastWriteUtc = $item.LastWriteTimeUtc
        Endpoint = $endpoint
    }
    return $endpoint
}

function Get-ProjectApiEndpoint {
    param(
        [string]$ProjectName,
        [string]$ProjectDir,
        $State
    )

    $stateCandidates = @()
    if ($null -ne $State) {
        $stateCandidates += @(
            $State.current_api_endpoint,
            $State.current_api_endpoint_base_url,
            $State.configured_primary_api_endpoint,
            $State.configured_primary_api_endpoint_base_url
        )
    }

    foreach ($candidate in $stateCandidates) {
        $display = Format-ApiEndpointDisplay -Value ([string]$candidate)
        if ($display -and $display -ne '-') {
            return $display
        }
    }

    $heartbeatPath = Join-Path $ProjectDir 'logs\runner_heartbeat.json'
    if (Test-Path $heartbeatPath) {
        try {
            $heartbeat = Get-Content -LiteralPath $heartbeatPath -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($candidate in @($heartbeat.current_api_endpoint, $heartbeat.current_api_endpoint_base_url, $heartbeat.configured_primary_api_endpoint, $heartbeat.configured_primary_api_endpoint_base_url)) {
                $display = Format-ApiEndpointDisplay -Value ([string]$candidate)
                if ($display -and $display -ne '-') {
                    return $display
                }
            }
        } catch {
        }
    }

    return Get-ProjectApiEndpointFromLog -ProjectName $ProjectName -ProjectDir $ProjectDir
}

function Update-History {
    param(
        [string]$ProjectName,
        [datetime]$Now,
        [int64]$BodyChars
    )

    if (-not $script:History.ContainsKey($ProjectName)) {
        $script:History[$ProjectName] = New-Object System.Collections.ArrayList
    }

    $bucket = $script:History[$ProjectName]
    [void]$bucket.Add([pscustomobject]@{
        Time = $Now
        BodyChars = $BodyChars
    })

    while ($bucket.Count -gt 0 -and ($Now - $bucket[0].Time).TotalSeconds -gt 21600) {
        $bucket.RemoveAt(0)
    }
}

function Get-RateSnapshot {
    param(
        [string]$ProjectName,
        [datetime]$Now,
        [int64]$CurrentBodyChars
    )

    $bucket = $script:History[$ProjectName]
    if (-not $bucket -or $bucket.Count -lt 2) {
        return @{
            BodyPerHour = 0L
            BodyPerMinute = 0.0
            ObservationSeconds = 0
        }
    }

    $first = $bucket[0]
    $elapsedSeconds = ($Now - $first.Time).TotalSeconds
    $elapsedHours = ($Now - $first.Time).TotalHours
    $elapsedMinutes = ($Now - $first.Time).TotalMinutes
    if ($elapsedHours -le 0 -or $elapsedMinutes -le 0 -or $elapsedSeconds -le 0) {
        return @{
            BodyPerHour = 0L
            BodyPerMinute = 0.0
            ObservationSeconds = 0
        }
    }

    [int64]$bodyDelta = [Math]::Max(0, $CurrentBodyChars - [int64]$first.BodyChars)

    return @{
        BodyPerHour = [int64][Math]::Round($bodyDelta / $elapsedHours)
        BodyPerMinute = [math]::Round($bodyDelta / $elapsedMinutes, 1)
        ObservationSeconds = [int][math]::Round($elapsedSeconds)
    }
}

function Format-Wan {
    param([int64]$Value)
    return ('{0:N2}w' -f ($Value / 10000.0))
}

function Format-Rate {
    param([int64]$Value)
    return ('{0:N0}/h' -f $Value)
}

function Format-RateMinute {
    param([double]$Value)
    return ('{0:N1}/m' -f $Value)
}

function Format-Window {
    param([int]$Seconds)

    if ($Seconds -le 0) {
        return '00s'
    }

    $ts = [TimeSpan]::FromSeconds($Seconds)
    if ($ts.TotalHours -ge 1) {
        return ('{0:D2}h{1:D2}m' -f [int]$ts.TotalHours, $ts.Minutes)
    }
    if ($ts.TotalMinutes -ge 1) {
        return ('{0:D2}m{1:D2}s' -f [int]$ts.TotalMinutes, $ts.Seconds)
    }
    return ('{0:D2}s' -f [int]$ts.TotalSeconds)
}

function Format-ApiEndpointDisplay {
    param([string]$Value)

    $text = ''
    if ($null -ne $Value) {
        $text = [string]$Value
    }
    $text = $text.Trim()
    if (-not $text) {
        return '-'
    }

    if ($text.Contains('|')) {
        $parts = @($text.Split('|'))
        $text = [string]$parts[$parts.Count - 1]
        $text = $text.Trim()
    }

    try {
        $uri = [System.Uri]$text
        $display = ($uri.Host + $uri.AbsolutePath).Trim()
        if ($display) {
            return $display.TrimEnd('/')
        }
    } catch {
    }

    return $text.TrimEnd('/')
}

function Get-ApiChainOrderDisplay {
    $chainFile = if ($env:SUB2API_API_CHAIN_FILE) {
        $env:SUB2API_API_CHAIN_FILE
    } else {
        Join-Path $env:USERPROFILE '.codex\sub2api_priority.json'
    }

    if (-not $chainFile) {
        return @()
    }

    if (-not (Test-Path $chainFile)) {
        return @()
    }

    $item = Get-Item $chainFile
    $cached = $script:ApiChainOrderCache
    if ($cached -and $cached.Path -eq $item.FullName -and $cached.Length -eq $item.Length -and $cached.LastWriteUtc -eq $item.LastWriteTimeUtc) {
        return @($cached.Order)
    }

    try {
        $payload = Get-Content -LiteralPath $chainFile -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return @()
    }

    $providers = @()
    if ($payload -and $payload.providers) {
        $providers = @($payload.providers)
    } elseif ($payload -and $payload.endpoints) {
        $providers = @($payload.endpoints)
    } elseif ($payload -and $payload.chain) {
        $providers = @($payload.chain)
    }

    $order = @()
    foreach ($provider in $providers) {
        $name = ''
        if ($null -ne $provider.name) {
            $name = ([string]$provider.name).Trim()
        } elseif ($null -ne $provider.label) {
            $name = ([string]$provider.label).Trim()
        } elseif ($null -ne $provider.provider) {
            $name = ([string]$provider.provider).Trim()
        }
        $baseUrl = ''
        if ($null -ne $provider.base_url) {
            $baseUrl = [string]$provider.base_url
        } elseif ($null -ne $provider.endpoint) {
            $baseUrl = [string]$provider.endpoint
        } elseif ($null -ne $provider.url) {
            $baseUrl = [string]$provider.url
        }
        $display = Format-ApiEndpointDisplay -Value $baseUrl
        if ($display -and $display -ne '-') {
            if ($name) {
                $display = ('{0}={1}' -f $name, $display)
            }
            $order += $display
        }
    }

    $script:ApiChainOrderCache = @{
        Path = $item.FullName
        Length = $item.Length
        LastWriteUtc = $item.LastWriteTimeUtc
        Order = @($order)
    }
    return @($order)
}

while ($true) {
    $now = Get-Date
    $projectsRoot = Join-Path $RepoRoot 'auto_projects'
    $runningProjects = Get-RunningProjects
    $rows = @()
    $seen = @{}

    foreach ($projectDir in Get-ChildItem $projectsRoot -Directory | Sort-Object Name) {
        $statePath = Join-Path $projectDir.FullName 'state.json'
        if (-not (Test-Path $statePath)) {
            continue
        }

        try {
            $state = Get-Content $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        } catch {
            continue
        }

        if ($state.status -eq 'completed') {
            continue
        }

        $projectName = $projectDir.Name
        $seen[$projectName] = $true

        [int64]$bodyChars = if ($null -ne $state.generated_chars) { $state.generated_chars } else { 0 }
        [int64]$targetChars = if ($null -ne $state.target_chars) { $state.target_chars } else { 0 }
        Update-History -ProjectName $projectName -Now $now -BodyChars $bodyChars
        $rate = Get-RateSnapshot -ProjectName $projectName -Now $now -CurrentBodyChars $bodyChars

        $rows += [pscustomobject]@{
            project = $projectName
            run = if ($runningProjects[$projectName]) { 'Y' } else { 'N' }
            body = $bodyChars
            target = $targetChars
            pct = if ($targetChars -gt 0) { '{0,6:N2}%' -f (($bodyChars / [double]$targetChars) * 100) } else { '   n/a' }
            bodyPerMinute = [double]$rate.BodyPerMinute
            bodyPerHour = [int64]$rate.BodyPerHour
            observationSeconds = [int]$rate.ObservationSeconds
            chapters = if ($null -ne $state.generated_chapters) { [int]$state.generated_chapters } else { 0 }
            api = Get-ProjectApiEndpoint -ProjectName $projectName -ProjectDir $projectDir.FullName -State $state
            stage = [string]$state.current_stage
            updated = [string]$state.updated_at
        }
    }

    foreach ($key in @($script:History.Keys)) {
        if (-not $seen[$key]) {
            $script:History.Remove($key)
        }
    }
    foreach ($key in @($script:LogCache.Keys)) {
        if (-not $seen[$key]) {
            $script:LogCache.Remove($key)
        }
    }

    Clear-Host
    Write-Host "Novel Progress Monitor  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  refresh=${RefreshSeconds}s"
    Write-Host "Repo: $RepoRoot"
    Write-Host ""

    if (-not $rows -or $rows.Count -eq 0) {
        Write-Host "No unfinished projects with state.json found."
    } else {
        $displayRows = $rows |
            Sort-Object @{ Expression = { if ($_.run -eq 'Y') { 0 } else { 1 } } }, project |
            Select-Object `
                @{ Name = 'project'; Expression = { $_.project } }, `
                @{ Name = 'run'; Expression = { $_.run } }, `
                @{ Name = 'body'; Expression = { Format-Wan $_.body } }, `
                @{ Name = 'target'; Expression = { Format-Wan $_.target } }, `
                @{ Name = 'pct'; Expression = { $_.pct } }, `
                @{ Name = 'body/m'; Expression = { Format-RateMinute $_.bodyPerMinute } }, `
                @{ Name = 'body/h'; Expression = { Format-Rate $_.bodyPerHour } }, `
                @{ Name = 'rate_win'; Expression = { Format-Window $_.observationSeconds } }, `
                @{ Name = 'chap'; Expression = { $_.chapters } }, `
                @{ Name = 'api'; Expression = { $_.api } }, `
                @{ Name = 'stage'; Expression = { $_.stage } }

        $displayRows | Format-Table -AutoSize

        [int64]$sumBody = ($rows | Measure-Object -Property body -Sum).Sum
        [int64]$sumTarget = ($rows | Measure-Object -Property target -Sum).Sum
        [double]$sumBodyPerMinute = ($rows | Measure-Object -Property bodyPerMinute -Sum).Sum
        [int64]$sumBodyPerHour = ($rows | Measure-Object -Property bodyPerHour -Sum).Sum
        $runningCount = ($rows | Where-Object { $_.run -eq 'Y' }).Count

        Write-Host ""
        Write-Host ('Unfinished projects : {0}' -f $rows.Count)
        Write-Host ('Running now          : {0}' -f $runningCount)
        Write-Host ('Body chars total     : {0} ({1})' -f ('{0:N0}' -f $sumBody), (Format-Wan $sumBody))
        Write-Host ('Target chars total   : {0} ({1})' -f ('{0:N0}' -f $sumTarget), (Format-Wan $sumTarget))
        Write-Host ('Body speed total     : {0} | {1}' -f (Format-RateMinute $sumBodyPerMinute), (Format-Rate $sumBodyPerHour))
        Write-Host ""
        $apiOrder = @(Get-ApiChainOrderDisplay)
        if ($apiOrder.Count -gt 0) {
            Write-Host ('API endpoint order   : {0}' -f (($apiOrder | ForEach-Object -Begin { $i = 0 } -Process { $i += 1; '{0}. {1}' -f $i, $_ }) -join '  ->  '))
            Write-Host ""
        }
        Write-Host 'Legend: body = manuscript chars tracked by state.json; rate_win = current speed observation window, not an auto-stop countdown'
    }

    if ($Once) {
        break
    }
    Start-Sleep -Seconds $RefreshSeconds
}
