# scripts/run_telegram_listener.ps1 — Windows / PowerShell
#
# Arranca el listener de Telegram (Telethon) según .env.
# Si TELEGRAM_ENABLED=false termina sin conectar.
#
# Uso:
#   .\scripts\run_telegram_listener.ps1               # listen continuo
#   .\scripts\run_telegram_listener.ps1 -Once         # escucha 1 mensaje y termina
#   .\scripts\run_telegram_listener.ps1 -Backfill -Limit 50

[CmdletBinding()]
param(
    [switch]$Once,
    [switch]$Backfill,
    [int]$Limit = 0
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

if ($Backfill) {
    $cmd = @("-m", "ofertas_hunter", "telegram-backfill")
    if ($Limit -gt 0) { $cmd += "--limit"; $cmd += "$Limit" }
} else {
    $cmd = @("-m", "ofertas_hunter", "telegram-listen")
    if ($Once) { $cmd += "--once" }
    if ($Limit -gt 0) { $cmd += "--limit"; $cmd += "$Limit" }
}

& $python @cmd
