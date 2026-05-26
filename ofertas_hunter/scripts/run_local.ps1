# scripts/run_local.ps1
#
# Arranca el dispatcher en Windows / PowerShell durante desarrollo.
# Por defecto respeta PUBLISHING_DRY_RUN/PUBLISHING_ENABLED del .env.
#
# Uso:
#   .\scripts\run_local.ps1                  # loop forever
#   .\scripts\run_local.ps1 -Once            # un solo tick
#   .\scripts\run_local.ps1 -Sample normal   # encola sample antes de arrancar
#   .\scripts\run_local.ps1 -Sample price_error -Once

[CmdletBinding()]
param(
    [switch]$Once,
    [ValidateSet("normal", "price_error", "")]
    [string]$Sample = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
Set-Location $ProjectRoot

$python = if (Test-Path ".\.venv\Scripts\python.exe") {
    ".\.venv\Scripts\python.exe"
} else {
    "python"
}

Write-Host "[run_local] cwd=$ProjectRoot" -ForegroundColor Cyan
Write-Host "[run_local] python=$python" -ForegroundColor Cyan

if ($Sample -ne "") {
    & $python -m ofertas_hunter enqueue-sample --kind $Sample
}

$dispatchArgs = @("-m", "ofertas_hunter", "dispatch")
if ($Once) { $dispatchArgs += "--once" }

& $python @dispatchArgs
