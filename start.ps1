# start.ps1 (atajo) - Redirige al lanzador real en ofertas_hunter\start.ps1
# Para que `.\start.ps1` funcione desde la carpeta padre.

$Real = Join-Path $PSScriptRoot "ofertas_hunter\start.ps1"
if (-not (Test-Path $Real)) {
    Write-Host "ERROR: no se encuentra $Real" -ForegroundColor Red
    exit 1
}
& $Real
