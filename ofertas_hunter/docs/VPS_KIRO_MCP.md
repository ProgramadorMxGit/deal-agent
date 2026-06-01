# VPS — Modos de operación: Producción daemon vs Kiro/MCP

Esta VPS (`34.59.242.95`, usuario `agaetranahoy`) corre `ofertas_hunter` en
producción 24/7 vía systemd. Kiro/MCP queda disponible como modo manual
de supervisión cuando el operador lo necesite, pero **no de forma
simultánea** — el `mcp_serve.lock` lo impide explícitamente.

## 1. Modo producción (default, 24/7)

```bash
sudo systemctl status ofertas-hunter
sudo systemctl restart ofertas-hunter
sudo journalctl -u ofertas-hunter -f
```

El servicio ejecuta `python -m ofertas_hunter run` que es el orchestrator
nativo. Loops paralelos: Amazon hunter, ML hunter, Telegram listener,
dispatcher de outbox a WhatsApp. Respeta scheduler nocturno.

Reinicia automáticamente si falla (`Restart=always`, `RestartSec=15`).

### Scripts de operación

```bash
cd /opt/deal-agent/ofertas_hunter

./scripts/vps_status.sh    # estado servicio + bot
./scripts/vps_logs.sh      # journalctl tail -f
./scripts/vps_restart.sh   # reiniciar
./scripts/vps_stop.sh      # detener
./scripts/vps_update.sh    # git pull + pip install + tests + restart
./scripts/vps_backup.sh    # backup de .env + DB + secrets en backups/
```

## 2. Modo Kiro / MCP (manual, supervisión IA)

Para usar Kiro (`mcp-serve`) hay que **detener primero el daemon**, porque
el lockfile `data/mcp_serve.lock` es exclusivo:

```bash
sudo systemctl stop ofertas-hunter
cd /opt/deal-agent/ofertas_hunter
source .venv/bin/activate
python -m ofertas_hunter mcp-serve
```

Eso arranca el servidor MCP por stdio. Conectas tu cliente Kiro CLI
contra él (mismo set de tools, misma DB, mismo `.env`).

Cuando termines:

```
Ctrl+C  # detiene mcp-serve
sudo systemctl start ofertas-hunter
```

## 3. Cómo alternar entre modos

| De → A | Comandos |
|---|---|
| daemon → mcp | `sudo systemctl stop ofertas-hunter` → `python -m ofertas_hunter mcp-serve` |
| mcp → daemon | `Ctrl+C` en mcp-serve → `sudo systemctl start ofertas-hunter` |

## 4. Cómo revisar lockfile

```bash
ls -la /opt/deal-agent/ofertas_hunter/data/mcp_serve.lock
```

Si existe pero no hay proceso vivo (después de un crash):
```bash
sudo systemctl stop ofertas-hunter
rm /opt/deal-agent/ofertas_hunter/data/mcp_serve.lock
sudo systemctl start ofertas-hunter
```

## 5. Login visual remoto en VPS (si las sesiones de browser caducan)

ML/Amazon usan `secrets/browser_profiles/<marketplace>/`. Si ML invalida
la sesión y te pide re-login en VPS sin GUI, usa Xvfb + x11vnc + túnel SSH:

```bash
# En VPS:
sudo apt install -y xvfb x11vnc fluxbox
Xvfb :99 -screen 0 1366x768x24 &
fluxbox -display :99 &
x11vnc -display :99 -nopw -listen localhost -xkb &

# En tu máquina local:
ssh -i ~/.ssh/gcp_ofertas_bot -L 5900:localhost:5900 agaetranahoy@34.59.242.95
# Conecta tu visor VNC a localhost:5900

# En VPS dentro del túnel:
cd /opt/deal-agent/ofertas_hunter
source .venv/bin/activate
DISPLAY=:99 python -m ofertas_hunter login --marketplace mercadolibre
```

## 6. Verificación rápida

```bash
sudo systemctl is-active ofertas-hunter   # debe imprimir "active"
sudo systemctl is-enabled ofertas-hunter  # debe imprimir "enabled"
journalctl -u ofertas-hunter -n 50 --no-pager | grep -E 'amazon hunt:|ml hunt:|publish'
```

## 7. Comandos útiles MCP

Cuando estás en modo MCP:

```bash
# Ver tools registradas
python -m ofertas_hunter mcp-serve --help

# Validación quick (no inicia el server)
python -m ofertas_hunter check-config
python -m ofertas_hunter status
python -m ofertas_hunter check-db
```
