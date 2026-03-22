param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectTag,

    [string]$CriticModel = 'gpt/gpt-5.4',
    [string]$CriticReasoningEffort = 'high',
    [int]$CriticMaxPasses = 3,
    [string]$EndingPolishModel = 'gpt/gpt-5.4',
    [string]$EndingPolishReasoningEffort = 'xhigh',
    [int]$MaxCycles = 3,
    [int]$MaxRetries = 0,
    [int]$RetryBackoffSeconds = 15
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = "Ending Repair - $ProjectTag"

$projectDir = Join-Path $repoRoot ("auto_projects\" + $ProjectTag)
$logsDir = Join-Path $projectDir 'logs'
$visibleLogPath = Join-Path $logsDir 'ending_quality_repair_visible.log'
$pythonCmd = Join-Path $repoRoot '.venv\Scripts\python.exe'

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

if (-not (Test-Path $pythonCmd)) {
    $pythonCmd = (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1)
    if (-not $pythonCmd) {
        throw 'Python virtualenv not found, and no python was found on PATH.'
    }
}

if (-not $env:GPT_API_KEY) {
    $authPath = Join-Path $env:USERPROFILE '.codex\auth.json'
    if (Test-Path $authPath) {
        $auth = Get-Content $authPath -Raw | ConvertFrom-Json
        if ($auth.OPENAI_API_KEY) {
            $env:GPT_API_KEY = [string]$auth.OPENAI_API_KEY
        }
    }
}

if (-not $env:GPT_API_KEY) {
    throw 'GPT_API_KEY is not set and could not be loaded from %USERPROFILE%\.codex\auth.json.'
}

if (-not $env:GPT_BASE_URL) {
    $env:GPT_BASE_URL = 'https://fast.vpsairobot.com/v1'
}

if ($env:GPT_BASE_URL -eq 'https://vpsairobot.com') {
    $env:GPT_BASE_URL = 'https://vpsairobot.com/v1'
}

if ($env:GPT_BASE_URL -eq 'https://fast.vpsairobot.com') {
    $env:GPT_BASE_URL = 'https://fast.vpsairobot.com/v1'
}

if (-not $env:GPT_AVAILABLE_MODELS) {
    $env:GPT_AVAILABLE_MODELS = 'gpt-5.4'
}

if (-not $env:GPT_MAX_INPUT_TOKENS) {
    $env:GPT_MAX_INPUT_TOKENS = '350000'
}

if (-not $env:GPT_MAX_OUTPUT_TOKENS) {
    $env:GPT_MAX_OUTPUT_TOKENS = '65536'
}

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$argsList = @(
    '-X', 'utf8',
    'scripts\ending_quality_repair.py',
    '--project-dir', ("auto_projects\" + $ProjectTag),
    '--critic-model', $CriticModel,
    '--critic-reasoning-effort', $CriticReasoningEffort,
    '--critic-max-passes', [string]$CriticMaxPasses,
    '--ending-polish-model', $EndingPolishModel,
    '--ending-polish-reasoning-effort', $EndingPolishReasoningEffort,
    '--max-cycles', [string]$MaxCycles,
    '--max-retries', [string]$MaxRetries,
    '--retry-backoff-seconds', [string]$RetryBackoffSeconds,
    '--live-stream'
)

Write-Host "Starting visible ending repair..."
Write-Host "Project: $projectDir"
Write-Host "Log: $visibleLogPath"
Write-Host "Model: $CriticModel / $EndingPolishModel"
Write-Host "Critic: $CriticReasoningEffort, max passes $CriticMaxPasses"
Write-Host "Ending polish: $EndingPolishReasoningEffort, max cycles $MaxCycles"
Write-Host "Base URL: $env:GPT_BASE_URL"
Write-Host
Write-Host 'This window shows live repair output.'
Write-Host 'Close this window to stop the repair.'
Write-Host

$oldErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    & $pythonCmd @argsList 2>&1 | Tee-Object -FilePath $visibleLogPath
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) {
        $exitCode = 0
    }
    exit $exitCode
}
finally {
    $ErrorActionPreference = $oldErrorActionPreference
}
