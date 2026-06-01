# Valida sintaxis de start.ps1 sin ejecutarlo
$err = $null
$tokens = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    "$PSScriptRoot\..\start.ps1",
    [ref]$tokens,
    [ref]$err
)
if ($err -and $err.Count -gt 0) {
    $err | ForEach-Object { Write-Host $_.Message -ForegroundColor Red }
    exit 1
}
Write-Host "start.ps1 syntax OK" -ForegroundColor Green
exit 0
