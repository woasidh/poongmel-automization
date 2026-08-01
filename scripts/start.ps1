$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$prefectPath = Join-Path $projectRoot ".venv\Scripts\prefect.exe"
$runtimePath = Join-Path $projectRoot "runtime"
$logPath = Join-Path $runtimePath "logs"
$pidPath = Join-Path $runtimePath "pids.json"
$prefectHome = Join-Path $runtimePath "prefect"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Run scripts/setup.ps1 first."
}

if (Test-Path -LiteralPath $pidPath) {
    $oldPids = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
    $alive = @($oldPids.PSObject.Properties.Value | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
    if ($alive.Count -gt 0) {
        throw "Pungmail processes are already running. Check scripts/status.ps1."
    }
}

New-Item -ItemType Directory -Path $logPath,$prefectHome -Force | Out-Null
$env:PREFECT_HOME = $prefectHome
$env:PREFECT_API_URL = "http://127.0.0.1:4200/api"
$env:PUNGMAIL_PREFECT_HOME = $prefectHome
$env:PUNGMAIL_PREFECT_API_URL = $env:PREFECT_API_URL
$env:PUNGMAIL_PROJECT_ROOT = $projectRoot

Push-Location $projectRoot
try {
    & $pythonPath -m pungmail.cli db-upgrade

    $server = Start-Process -FilePath $prefectPath -ArgumentList @("server","start","--host","127.0.0.1","--port","4200") -WorkingDirectory $projectRoot -RedirectStandardOutput (Join-Path $logPath "prefect-server.stdout.log") -RedirectStandardError (Join-Path $logPath "prefect-server.stderr.log") -WindowStyle Hidden -PassThru

    $prefectReady = $false
    for ($attempt = 0; $attempt -lt 45; $attempt++) {
        Start-Sleep -Seconds 1
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:4200/api/health" -UseBasicParsing -TimeoutSec 1
            if ($response.StatusCode -eq 200) { $prefectReady = $true; break }
        } catch { }
    }
    if (-not $prefectReady) {
        Stop-Process -Id $server.Id -ErrorAction SilentlyContinue
        throw "Prefect did not become ready. Check runtime/logs/prefect-server.stderr.log."
    }

    & $prefectPath work-pool create "pungmail-local" --type process --overwrite
    & $prefectPath deploy --all

    $worker = Start-Process -FilePath $prefectPath -ArgumentList @("worker","start","--pool","pungmail-local") -WorkingDirectory $projectRoot -RedirectStandardOutput (Join-Path $logPath "prefect-worker.stdout.log") -RedirectStandardError (Join-Path $logPath "prefect-worker.stderr.log") -WindowStyle Hidden -PassThru
    $ui = Start-Process -FilePath $pythonPath -ArgumentList @("-m","pungmail.cli","serve-ui","--host","127.0.0.1","--port","8000") -WorkingDirectory $projectRoot -RedirectStandardOutput (Join-Path $logPath "ui.stdout.log") -RedirectStandardError (Join-Path $logPath "ui.stderr.log") -WindowStyle Hidden -PassThru

    $pids = [ordered]@{ prefect_server = $server.Id; prefect_worker = $worker.Id; management_ui = $ui.Id }
    $pids | ConvertTo-Json | Set-Content -LiteralPath $pidPath -Encoding utf8

    $uiReady = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 1
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/healthz" -TimeoutSec 1
            if ($health.database) { $uiReady = $true; break }
        } catch { }
    }
    if (-not $uiReady) {
        throw "The management UI did not become ready. Check runtime/logs/ui.stderr.log."
    }
} catch {
    if (Test-Path -LiteralPath $pidPath) {
        & (Join-Path $PSScriptRoot "stop.ps1")
    }
    throw
} finally {
    Pop-Location
}

Write-Host "Pungmail is running."
Write-Host "Management UI: http://127.0.0.1:8000"
Write-Host "Prefect UI: http://127.0.0.1:4200"
