$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) { throw "Run scripts/setup.ps1 first." }
Push-Location $projectRoot
try {
    & $pythonPath -m pungmail.cli seed-issue2-demo
    if ($LASTEXITCODE -ne 0) { throw "Issue 2 demo seed failed with exit code $LASTEXITCODE." }
} finally { Pop-Location }
Write-Host "Open the management UI and check Work, AI Decisions, Run History, and Outbox."
