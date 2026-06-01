# Mantenimiento nocturno (nightly maintenance)

Rutina segura que aprovecha la hibernación nocturna del bot (cuando NO
publica) para purgar `runtime_events`, compactar la DB con `VACUUM`, verificar
integridad y reiniciar el servicio limpio.

## Qué hace (en orden)

1. **Ventana segura**: confirma que estamos dentro de
   `NIGHTLY_MAINTENANCE_START`–`END` (TZ `America/Mexico_City`) y que no hay
   locks de perfil Amazon/ML activos. Fuera de ventana → `skipped` (salvo
   `--force-window`).
2. **quick_check** sobre la DB activa. Si falla, aborta sin tocar nada.
3. **Detiene** `ofertas-hunter.service` (acceso exclusivo para VACUUM).
4. **Purga** `runtime_events` con `kind='mcp_tool_called'` más viejos que
   `RUNTIME_EVENTS_KEEP_MCP_TOOL_CALLED_HOURS` (48h), en batches.
5. **Checkpoint WAL + VACUUM** (solo si hay ≥ `NIGHTLY_VACUUM_MIN_FREE_GB`
   libres y `quick_check` fue OK).
6. **integrity_check**. Si falla, **NO reinicia** (deja el bot detenido y
   marca el run como fallido).
7. **Reinicia** `ofertas-hunter.service` (si `NIGHTLY_MAINTENANCE_RESTART_AFTER`).
8. Emite `nightly_maintenance_summary` en `runtime_events`.

**Nunca toca**: outbox, published_messages, frontier, products, offers,
discarded_candidates, gates, cookies ni perfiles. **No publica nada.**

## CLI

```bash
# Dry-run (default si no pasas --run). Cuenta candidatos, no borra nada.
python -m ofertas_hunter nightly-maintenance --dry-run --force-window

# Ejecución real sin reiniciar (prueba controlada con el bot ya detenido):
python -m ofertas_hunter nightly-maintenance --run --force-window --no-restart

# Ejecución real con restart (lo que hace el timer):
python -m ofertas_hunter nightly-maintenance --run --force-window

# Salida JSON (para journald/scripts):
python -m ofertas_hunter nightly-maintenance --run --json
```

Flags: `--run`, `--dry-run`, `--force-window`, `--skip-vacuum`,
`--no-restart`, `--max-seconds N`, `--batch-size N`, `--backup`, `--json`.

## Configuración (.env)

```dotenv
NIGHTLY_MAINTENANCE_ENABLED=true
NIGHTLY_MAINTENANCE_START=03:00
NIGHTLY_MAINTENANCE_END=04:30
NIGHTLY_MAINTENANCE_TIMEZONE=America/Mexico_City
RUNTIME_EVENTS_KEEP_MCP_TOOL_CALLED_HOURS=48
RUNTIME_EVENTS_KEEP_VERBOSE_DAYS=7
NIGHTLY_VACUUM_ENABLED=true
NIGHTLY_VACUUM_MIN_FREE_GB=10
NIGHTLY_MAINTENANCE_BATCH_SIZE=100000
NIGHTLY_MAINTENANCE_RESTART_AFTER=true
NIGHTLY_MAINTENANCE_MAX_SECONDS=3600
NIGHTLY_MAINTENANCE_CREATE_BACKUP=false
NIGHTLY_MAINTENANCE_BACKUP_RETENTION=1
NIGHTLY_MAINTENANCE_SERVICE_NAME=ofertas-hunter.service
```

## Operación del servicio principal (systemd)

El bot corre como `ofertas-hunter.service` (migrado de tmux a systemd).
`ExecStart` = `scripts/orquestador_ia.py` (exactamente el flujo de
`start.sh [3] -> [f]`). Cheatsheet:

```bash
# Estado / logs en vivo
systemctl status ofertas-hunter.service
journalctl -u ofertas-hunter.service -f

# Control
sudo systemctl start   ofertas-hunter.service
sudo systemctl stop    ofertas-hunter.service
sudo systemctl restart ofertas-hunter.service

# Boot
systemctl is-enabled ofertas-hunter.service   # -> enabled
```

> El unit fija `Environment=HOME=/home/agaetranahoy` para que Playwright
> resuelva Chromium en `~/.cache/ms-playwright` (igual que en tmux). NO usa
> `ProtectHome` ni fuerza `PLAYWRIGHT_BROWSERS_PATH` (eso rompía el browser
> en el unit anterior → watchdog/heartbeat stale).

## Instalación systemd (producción)

> ⚠️ **Requisito**: el servicio principal `ofertas-hunter.service` debe estar
> instalado, probado y `enabled` ANTES de habilitar el timer.

### 1. Servicio principal

```bash
sudo cp deploy/systemd/ofertas-hunter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ofertas-hunter.service
systemctl status ofertas-hunter.service
```

> Asegúrate de NO tener además el bot corriendo en tmux (doble publicación).

### 2. Timer de mantenimiento

```bash
sudo cp deploy/systemd/ofertas-hunter-nightly-maintenance.service /etc/systemd/system/
sudo cp deploy/systemd/ofertas-hunter-nightly-maintenance.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ofertas-hunter-nightly-maintenance.timer
```

> El `.service` corre como `agaetranahoy` y usa `sudo -n systemctl` para
> detener/arrancar el servicio principal
> (`NIGHTLY_MAINTENANCE_USE_SUDO=true`). Requiere sudo sin password para ese
> usuario (ya disponible en este VPS). Así los archivos DB/WAL conservan el
> owner correcto del bot.

### Verificación

```bash
systemctl status ofertas-hunter-nightly-maintenance.timer
systemctl list-timers | grep ofertas-hunter
journalctl -u ofertas-hunter-nightly-maintenance.service -n 100 --no-pager
```

## Pruebas manuales

```bash
# Dry-run (no toca nada, bot puede seguir arriba):
python -m ofertas_hunter nightly-maintenance --dry-run --force-window

# Real sin reinicio (detiene el servicio, purga, VACUUM; NO lo levanta):
python -m ofertas_hunter nightly-maintenance --run --force-window --no-restart

# Real con reinicio (detiene, limpia y vuelve a arrancar el servicio):
python -m ofertas_hunter nightly-maintenance --run --force-window
```

## Desactivar el timer

```bash
sudo systemctl disable --now ofertas-hunter-nightly-maintenance.timer
```

## Notas de zona horaria (IMPORTANTE)

El servidor está en **UTC** (`timedatectl` => `Etc/UTC`). La ventana del
`.env` es `03:00–04:30 America/Mexico_City` = **09:00–10:30 UTC**, por eso el
timer dispara con `OnCalendar=*-*-* 09:10:00` (≈ 03:10 hora MX).

La salvaguarda real es el chequeo del CLI con `NIGHTLY_MAINTENANCE_TIMEZONE`:
aunque el `OnCalendar` dispare fuera de ventana, el CLI hace `skipped`
(`not_safe_window`). Si algún día se cambia el TZ del server a
`America/Mexico_City`, ajusta `OnCalendar` a `03:10:00`.

