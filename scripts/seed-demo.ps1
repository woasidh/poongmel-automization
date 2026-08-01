$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) { throw "Run scripts/setup.ps1 first." }
Push-Location $projectRoot
try {
    & $pythonPath -m pungmail.cli seed-demo
    if ($LASTEXITCODE -ne 0) { throw "Demo seed failed with exit code $LASTEXITCODE." }
} finally { Pop-Location }
Write-Host "Open the management UI and find demo-mail-001 under Mail Evidence."
