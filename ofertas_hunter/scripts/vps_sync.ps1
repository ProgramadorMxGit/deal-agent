# vps_sync.ps1 - Sincroniza el repo local completo al VPS.
# Excluye: .venv, data/, secrets/, __pycache__, .git, .pytest_cache
#
# Uso: .\scripts\vps_sync.ps1
# Uso dry-run: .\scripts\vps_sync.ps1 -DryRun

param([switch]$DryRun)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$SshKey  = "$env:USERPROFILE\.ssh\gcp_ofertas_bot"
$SshHost = "agaetranahoy@34.59.242.95"
$Local   = "C:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter"
$Remote  = "/opt/deal-agent/ofertas_hunter"

$Excludes = @(
    "--exclude=.venv/",
    "--exclude=data/*.db",
    "--exclude=data/*.db-shm",
    "--exclude=data/*.db-wal",
    "--exclude=data/debug/",
    "--exclude=data/telegram_images/",
    "--exclude=data/training/",
    "--exclude=secrets/browser_profiles/",
    "--exclude=secrets/mercadolibre_cookies*.json",
    "--exclude=secrets/cookies_backups/",
    "--exclude=__pycache__/",
    "--exclude=*.pyc",
    "--exclude=.git/",
    "--exclude=.pytest_cache/",
    "--exclude=.kiro/",
    "--exclude=*.egg-info/",
    "--exclude=dist/",
    "--exclude=build/"
)

$RsyncArgs = @(
    "-avz",
    "--checksum",
    "--progress"
) + $Excludes + @(
    "-e", "ssh -i `"$SshKey`"",
    "$Local/",
    "${SshHost}:${Remote}/"
)

if ($DryRun) {
    $RsyncArgs = @("--dry-run") + $RsyncArgs
    Write-Host "  [DRY-RUN] Archivos que se sincronizarían:" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  Sincronizando $Local → ${SshHost}:${Remote}" -ForegroundColor Cyan
Write-Host ""

# rsync via WSL si está disponible, sino usa scp manual
$rsync = Get-Command rsync -ErrorAction SilentlyContinue
if ($rsync) {
    & rsync @RsyncArgs
} else {
    Write-Host "  rsync no disponible. Usando scp para archivos clave..." -ForegroundColor Yellow
    $files = @(
        "src\ofertas_hunter\config.py",
        "src\ofertas_hunter\orchestrator.py",
        "src\ofertas_hunter\__main__.py",
        "src\ofertas_hunter\mcp\context.py",
        "src\ofertas_hunter\session\ml_session_recovery.py",
        "src\ofertas_hunter\session\ml_session_inbound.py",
        "src\ofertas_hunter\session\ml_session_runtime.py",
        "src\ofertas_hunter\session\ml_session_manager.py",
        "src\ofertas_hunter\session\ml_session_poller.py",
        "src\ofertas_hunter\session\mercadolibre_session.py",
        "src\ofertas_hunter\agents\mercadolibre_hunter_agent.py",
        "src\ofertas_hunter\agents\legacy_amazon_hunter_agent.py",
        "src\ofertas_hunter\agents\legacy_amazon\__init__.py",
        "src\ofertas_hunter\agents\legacy_amazon\worker.py",
        "src\ofertas_hunter\agents\legacy_amazon\adapter.py",
        "src\ofertas_hunter\agents\legacy_amazon\price_parser.py",
        "src\ofertas_hunter\agents\legacy_amazon\selectors.json",
        "src\ofertas_hunter\agents\legacy_amazon\discovery_browser_adapter.py",
        # Diversity Curator Agent (2026-05-28)
        "src\ofertas_hunter\dispatching\diversity_scorer.py",
        "src\ofertas_hunter\dispatching\diversity_curator.py",
        "src\ofertas_hunter\dispatching\curator_factory.py",
        "src\ofertas_hunter\dispatching\dispatcher.py",
        "src\ofertas_hunter\intelligence\kiro_cli_client.py",
        "src\ofertas_hunter\mcp\tools\action_tools.py",
        "tests\unit\dispatching\test_diversity_scorer.py",
        "tests\unit\dispatching\test_diversity_curator.py",
        "tests\unit\dispatching\test_dispatcher_with_selector.py",
        "tests\unit\intelligence\test_kiro_cli_client.py",
        # ML session recovery
        "tests\unit\session\test_ml_session_manager.py",
        "tests\unit\session\test_ml_session_poller.py",
        "tests\unit\session\test_ml_hot_reload_acceptance.py",
        "tests\unit\session\test_ml_session_runtime.py",
        "tests\unit\session\test_ml_session_inbound.py",
        "tests\unit\session\test_ml_session_recovery.py",
        "tests\unit\agents\test_mercadolibre_hunter_agent.py",
        "scripts\orquestador_ia.py",
        "scripts\_vps_check_recovery.py",
        "scripts\_vps_evolution_probe.sh",
        "start.sh",
        "changes.md"
    )
    foreach ($f in $files) {
        $src = Join-Path $Local $f
        $dst = $f.Replace("\", "/")
        if (Test-Path $src) {
            if (-not $DryRun) {
                scp -i $SshKey $src "${SshHost}:${Remote}/${dst}" 2>&1 | Out-Null
            }
            Write-Host "  [OK] $dst" -ForegroundColor Green
        }
    }
}

Write-Host ""
Write-Host "  Sync completado." -ForegroundColor Cyan
