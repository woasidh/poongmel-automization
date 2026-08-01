$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pidPath = Join-Path $projectRoot "runtime\pids.json"

function Stop-ProcessTree {
    param([int]$RootProcessId)
    $children = @(Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $RootProcessId })
    foreach ($child in $children) {
        Stop-ProcessTree -RootProcessId ([int]$child.ProcessId)
    }
    if (Get-Process -Id $RootProcessId -ErrorAction SilentlyContinue) {
        # A child can exit between discovery and termination; that race is harmless.
        Stop-Process -Id $RootProcessId -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Host "No recorded Pungmail processes."
    exit 0
}

$pids = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
foreach ($property in $pids.PSObject.Properties) {
    $processId = [int]$property.Value
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($process) {
        Stop-ProcessTree -RootProcessId $processId
        Write-Host "Stopped $($property.Name). PID=$processId"
    }
}

Remove-Item -LiteralPath $pidPath -Force
Write-Host "Pungmail processes stopped."
