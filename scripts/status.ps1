$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pidPath = Join-Path $projectRoot "runtime\pids.json"

Write-Host "Processes"
if (Test-Path -LiteralPath $pidPath) {
    $pids = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
    foreach ($property in $pids.PSObject.Properties) {
        $process = Get-Process -Id ([int]$property.Value) -ErrorAction SilentlyContinue
        $state = if ($process) { "RUNNING" } else { "STOPPED" }
        Write-Host ("- {0}: {1} (PID {2})" -f $property.Name,$state,$property.Value)
    }
} else {
    Write-Host "- No recorded processes"
}

Write-Host "Health"
foreach ($target in @(
    @{Name="Management UI"; Url="http://127.0.0.1:8000/healthz"},
    @{Name="Prefect"; Url="http://127.0.0.1:4200/api/health"}
)) {
    try {
        $response = Invoke-WebRequest -Uri $target.Url -UseBasicParsing -TimeoutSec 2
        Write-Host ("- {0}: OK ({1})" -f $target.Name,$response.StatusCode)
    } catch {
        Write-Host ("- {0}: NO RESPONSE" -f $target.Name)
    }
}
