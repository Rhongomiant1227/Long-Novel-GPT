$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

& "$root\run.ps1" -NoBrowser

try {
    $health = Invoke-RestMethod 'http://127.0.0.1:7869/health' -TimeoutSec 5
    $setting = Invoke-RestMethod 'http://127.0.0.1:7869/setting' -TimeoutSec 10

    Write-Host '--- Health ---'
    $health | ConvertTo-Json -Depth 5
    Write-Host '--- Setting ---'
    $setting | ConvertTo-Json -Depth 5
}
finally {
    & "$root\stop.bat"
}
