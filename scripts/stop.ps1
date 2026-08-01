$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pidPath = Join-Path $projectRoot "runtime\pids.json"

if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Host "No recorded Pungmail processes."
    exit 0
}

$pids = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
foreach ($property in $pids.PSObject.Properties) {
    $processId = [int]$property.Value
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -Id $processId -ErrorAction Stop
        Write-Host "Stopped $($property.Name). PID=$processId"
    }
}

Remove-Item -LiteralPath $pidPath -Force
Write-Host "Pungmail processes stopped."
