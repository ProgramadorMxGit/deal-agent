# start.ps1 - Lanzador del bot ofertas_hunter
# Uso: .\start.ps1

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$PYTHON   = ".\.venv\Scripts\python.exe"
$KIRO_CLI = "C:\Users\yarteaga\AppData\Local\Kiro-Cli\kiro-cli.exe"
$BOT_DIR  = $PSScriptRoot

Set-Location $BOT_DIR

# Limpiar lockfiles huerfanos
Remove-Item -Force "data\mcp_serve.lock" -ErrorAction SilentlyContinue
Remove-Item -Force "data\run.lock"       -ErrorAction SilentlyContinue

# Matar procesos previos del bot
Get-Process python -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*ofertas_hunter*' } |
    Stop-Process -Force -ErrorAction SilentlyContinue

Clear-Host

Write-Host ""
Write-Host "  =================================================" -ForegroundColor Cyan
Write-Host "       OFERTAS HUNTER - Lanzador" -ForegroundColor Cyan
Write-Host "  =================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  [1]  Modo IA - Orquestador principal (1 ventana)" -ForegroundColor Green
Write-Host "       kiro-cli con Claude Sonnet 4.5 + 16 tools MCP" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  [2]  Modo IA - Subagentes en paralelo (4 ventanas)" -ForegroundColor Magenta
Write-Host "       Amazon + ML + QA + Telegram cada uno en su terminal" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  [3]  Modo autonomo Python (sin IA, 24/7)" -ForegroundColor Yellow
Write-Host "       3 loops paralelos con reglas Python" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  [4]  Estado actual" -ForegroundColor Cyan
Write-Host "       Ver que esta pasando ahora mismo" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  [5]  Reinstalar agentes kiro-cli" -ForegroundColor DarkGray
Write-Host "       Recrea los 5 agentes en ~/.kiro/agents/" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  [Q]  Salir" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  =================================================" -ForegroundColor Cyan
Write-Host ""

$opcion = Read-Host "  Elige [1/2/3/4/5/Q]"

function Start-AgentInWindow {
    param(
        [string]$AgentName,
        [string]$Title,
        [string]$Color = "Green",
        [string]$Prompt = "Inicia tu ciclo continuo. Sin esperas entre iteraciones a menos que scheduler=hibernating."
    )
    $cmd = "`$Host.UI.RawUI.WindowTitle='$Title';" `
        + "Set-Location '$BOT_DIR';" `
        + "Write-Host '  >> $Title' -ForegroundColor $Color;" `
        + "Write-Host '';" `
        + "& '$KIRO_CLI' chat --agent $AgentName --trust-all-tools '$Prompt';" `
        + "Write-Host '';" `
        + "Read-Host '  Sesion terminada. Presiona Enter para cerrar'"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd | Out-Null
}

switch ($opcion.ToUpper()) {

    "1" {
        Write-Host ""
        Write-Host "  >> Abriendo kiro-cli - Orquestador principal..." -ForegroundColor Green
        Write-Host "  Agente: ofertas-orquestador" -ForegroundColor DarkGray
        Write-Host ""
        & $KIRO_CLI chat --agent ofertas-orquestador --trust-all-tools "Inicia tu ciclo continuo ahora mismo. Empieza con get_status y sigue con get_frontier_stats, discover_seeds, hunt_amazon, hunt_mercadolibre, dispatch_outbox, get_outbox + review borderline, get_recent_events. Sin esperas entre iteraciones."
    }

    "2" {
        Write-Host ""
        Write-Host "  >> Lanzando subagentes en paralelo (4 ventanas)..." -ForegroundColor Magenta
        Write-Host ""

        Start-AgentInWindow -AgentName "ofertas-amazon"  -Title "Amazon Hunter"   -Color "Green"
        Start-Sleep -Seconds 2
        Start-AgentInWindow -AgentName "ofertas-ml"      -Title "ML Hunter"       -Color "Yellow"
        Start-Sleep -Seconds 2
        Start-AgentInWindow -AgentName "ofertas-qa"      -Title "QA Reviewer"     -Color "Cyan"
        Start-Sleep -Seconds 2
        Start-AgentInWindow -AgentName "ofertas-telegram" -Title "Telegram Listener" -Color "Magenta"

        Write-Host ""
        Write-Host "  4 subagentes lanzados. Cada uno corre en su propia ventana." -ForegroundColor Green
        Write-Host ""
        Write-Host "  El dispatcher de outbox lo manejas con [1] Orquestador o" -ForegroundColor DarkGray
        Write-Host "  con [3] daemon Python." -ForegroundColor DarkGray
        Write-Host ""

        $resp = Read-Host "  Quieres lanzar tambien el orquestador (dispatch + supervision)? [s/N]"
        if ($resp -match '^[sSyY]') {
            Start-Sleep -Seconds 2
            Start-AgentInWindow -AgentName "ofertas-orquestador" `
                -Title "Orquestador Principal" `
                -Color "White" `
                -Prompt "Tu rol AHORA: dispatch_outbox cada vez que pase el cooldown + supervision via get_status/get_recent_events. NO hagas hunt: los subagentes Amazon, ML, QA, Telegram ya estan corriendo. Solo despachas y vigilas."
        }
        Write-Host ""
        Read-Host "  Presiona Enter para volver al menu"
        & "$BOT_DIR\start.ps1"
    }

    "3" {
        Write-Host ""
        Write-Host "  >> Arrancando daemon Python (3 loops paralelos)..." -ForegroundColor Yellow
        Write-Host "  Ctrl+C para detener." -ForegroundColor DarkGray
        Write-Host ""
        & $PYTHON scripts\orquestador_ia.py
    }

    "4" {
        Write-Host ""
        & $PYTHON -m ofertas_hunter status
        Write-Host ""
        Read-Host "  Presiona Enter para volver"
        & "$BOT_DIR\start.ps1"
    }

    "5" {
        Write-Host ""
        Write-Host "  >> Reinstalando agentes kiro-cli..." -ForegroundColor DarkGray
        Write-Host ""
        powershell -ExecutionPolicy Bypass -File "$BOT_DIR\scripts\install_kiro_agents.ps1"
        Write-Host ""
        Read-Host "  Presiona Enter para volver"
        & "$BOT_DIR\start.ps1"
    }

    "Q" {
        Write-Host ""
        Write-Host "  Hasta luego." -ForegroundColor DarkGray
        exit 0
    }

    default {
        Write-Host ""
        Write-Host "  Opcion no valida. Intenta de nuevo." -ForegroundColor Red
        Start-Sleep -Seconds 1
        & "$BOT_DIR\start.ps1"
    }
}
