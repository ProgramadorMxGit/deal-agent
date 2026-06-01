# run_bot.ps1 — Output en tiempo real + auto-relogin
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$KIRO = "C:\Users\Programador Mx\AppData\Local\kiro-cli\kiro-cli.exe"
$env:PYTHONIOENCODING = "utf-8"
$ciclo = 0
$promptBase = Get-Content ".\prompt.txt" -Raw -Encoding UTF8

function Test-KiroSession {
    $r = & $KIRO whoami 2>&1
    return ($r -join "") -match "Logged in"
}

Write-Host ""
Write-Host "  AMAZON OFFER HUNTER - claude-sonnet-4.5 - Kiro Pro" -ForegroundColor Cyan
Write-Host "  Ofertas >= 50pct -> oferts.json + Telegram" -ForegroundColor Gray
Write-Host ""

while ($true) {
    $ciclo++
    $hora = Get-Date -Format 'HH:mm:ss'

    Write-Host "`n========================================" -ForegroundColor Cyan
    Write-Host "  CICLO $ciclo - $hora" -ForegroundColor Cyan
    Write-Host "========================================`n" -ForegroundColor Cyan

    # Verificar sesion
    if (-not (Test-KiroSession)) {
        Write-Host "  [!] Sesion expirada - reconectando..." -ForegroundColor Red
        & $KIRO login --social google --use-device-flow
        Start-Sleep -Seconds 3
    }

    $prompt = "CICLO $ciclo. $promptBase"

    # Ejecutar en tiempo real con pipe (no capturar)
    & $KIRO chat --agent amazon-hunter --trust-all-tools --no-interactive $prompt 2>&1 | ForEach-Object {
        $line = [string]$_
        if ($line -match 'RemoteException|CategoryInfo|FullyQualified|trusted|risks|kiro\.dev|Agents can') { return }
        if ($line.Trim() -eq '') { return }

        if ($line -match '^\s*>\s+(.+)') {
            Write-Host "  [IA] " -ForegroundColor DarkYellow -NoNewline
            Write-Host ($Matches[1].Trim()) -ForegroundColor Yellow
        } elseif ($line -match 'Running tool (\S+)') {
            Write-Host "  [TOOL] $($Matches[1])" -ForegroundColor Cyan
        } elseif ($line -match 'OFERTA GUARDADA|% OFF') {
            Write-Host "  [OFERTA] $line" -ForegroundColor Green
        } elseif ($line -match 'Telegram|photo') {
            Write-Host "  [TELEGRAM] $line" -ForegroundColor Magenta
        } elseif ($line -match 'Credits:') {
            Write-Host "  $line" -ForegroundColor DarkYellow
        } elseif ($line -match 'Authentication failed|Not logged in|session may have expired') {
            Write-Host "  [!] Sesion expirada durante ciclo - reconectar con: kiro-cli login --social google --use-device-flow" -ForegroundColor Red
        } elseif ($line -match 'Error|CAPTCHA|503') {
            Write-Host "  [!] $line" -ForegroundColor Red
        } elseif ($line -match 'Completed in') {
            Write-Host "  $line" -ForegroundColor DarkGray
        } else {
            Write-Host "  $line" -ForegroundColor Gray
        }
    }

    Write-Host "`n  Ciclo $ciclo completado. Esperando 90 segundos..." -ForegroundColor Yellow
    Start-Sleep -Seconds 90
}
