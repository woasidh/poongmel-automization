$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvPath = Join-Path $projectRoot ".venv"
$pythonPath = Join-Path $venvPath "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    Write-Host "[1/4] Creating the Python virtual environment."
    python -m venv $venvPath
}

Write-Host "[2/4] Installing pinned dependencies."
& $pythonPath -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed with exit code $LASTEXITCODE." }
& $pythonPath -m pip install -e "$projectRoot[dev]"
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed with exit code $LASTEXITCODE." }

$envPath = Join-Path $projectRoot ".env"
$examplePath = Join-Path $projectRoot ".env.example"
if (-not (Test-Path -LiteralPath $envPath)) {
    Copy-Item -LiteralPath $examplePath -Destination $envPath
    Write-Host "[3/4] Created .env from the example configuration."
} else {
    Write-Host "[3/4] Keeping the existing .env configuration."
}

Write-Host "[4/4] Preparing the local database."
Push-Location $projectRoot
try {
    & $pythonPath -m pungmail.cli db-upgrade
    if ($LASTEXITCODE -ne 0) { throw "Database migration failed with exit code $LASTEXITCODE." }
} finally {
    Pop-Location
}

Write-Host "Setup complete. Run scripts/start.ps1."
