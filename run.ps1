[CmdletBinding()]
param(
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$runDir = Join-Path $root '.run'
$backendOut = Join-Path $runDir 'backend.out.log'
$backendErr = Join-Path $runDir 'backend.err.log'
$frontendOut = Join-Path $runDir 'frontend.out.log'
$frontendErr = Join-Path $runDir 'frontend.err.log'
$backendPidFile = Join-Path $runDir 'backend.pid'
$frontendPidFile = Join-Path $runDir 'frontend.pid'

function Get-PythonLauncher {
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @('python', '')
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @('py', '-3.10')
    }
    throw 'Python 3.10+ was not found. Please install Python and try again.'
}

if (-not (Test-Path $venvPython)) {
    $launcher = Get-PythonLauncher
    if ($launcher[1]) {
        & $launcher[0] $launcher[1] -m venv .venv
    }
    else {
        & $launcher[0] -m venv .venv
    }
}

New-Item -ItemType Directory -Force -Path $runDir | Out-Null

$depsStamp = Join-Path $root '.venv\.deps-installed'
$requirementsPath = Join-Path $root 'backend\requirements.txt'
$requirementsChanged = (-not (Test-Path $depsStamp)) -or ((Get-Item $requirementsPath).LastWriteTimeUtc -gt (Get-Item $depsStamp -ErrorAction SilentlyContinue).LastWriteTimeUtc)

if ($requirementsChanged) {
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r $requirementsPath
    Set-Content -Path $depsStamp -Value (Get-Date).ToString('o')
}

Get-Process python -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -like "$root*"
} | Stop-Process -Force -ErrorAction SilentlyContinue

Remove-Item $backendOut,$backendErr,$frontendOut,$frontendErr -Force -ErrorAction SilentlyContinue

$backendProc = Start-Process -FilePath $venvPython -ArgumentList 'backend\app.py' -WorkingDirectory $root -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr -PassThru
$frontendProc = Start-Process -FilePath $venvPython -ArgumentList '-m','http.server','8000','--directory','frontend' -WorkingDirectory $root -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr -PassThru

Set-Content -Path $backendPidFile -Value $backendProc.Id
Set-Content -Path $frontendPidFile -Value $frontendProc.Id

$healthUrl = 'http://127.0.0.1:7869/health'
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $resp = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
        if ($resp.status -eq 'healthy') {
            $ok = $true
            break
        }
    }
    catch {
    }
}

if (-not $ok) {
    Write-Host 'Backend failed to become healthy.'
    if (Test-Path $backendOut) { Write-Host '--- backend.out.log ---'; Get-Content -Tail 80 $backendOut }
    if (Test-Path $backendErr) { Write-Host '--- backend.err.log ---'; Get-Content -Tail 80 $backendErr }
    if (Test-Path $frontendOut) { Write-Host '--- frontend.out.log ---'; Get-Content -Tail 80 $frontendOut }
    if (Test-Path $frontendErr) { Write-Host '--- frontend.err.log ---'; Get-Content -Tail 80 $frontendErr }
    throw 'Startup failed.'
}

Write-Host ''
Write-Host 'Long-Novel-GPT is running.'
Write-Host 'Frontend: http://127.0.0.1:8000'
Write-Host 'Backend : http://127.0.0.1:7869'
Write-Host 'Health  : http://127.0.0.1:7869/health'
Write-Host ''
Write-Host 'If the browser does not open automatically, open the Frontend URL manually.'
if (-not $NoBrowser) {
    Start-Process 'http://127.0.0.1:8000'
}
