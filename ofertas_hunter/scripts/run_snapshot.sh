#!/bin/bash
# Wrapper para el snapshot horario de diversidad (read-only).
# Se auto-desinstala del cron después de STOP_EPOCH (24h tras instalación).
cd /opt/deal-agent/ofertas_hunter || exit 0
STOP_FILE="logs/.diversity_snapshot_stop_epoch"
NOW=$(date +%s)
if [ -f "$STOP_FILE" ]; then
  STOP=$(cat "$STOP_FILE")
  if [ "$NOW" -gt "$STOP" ]; then
    # ventana de 24h cumplida: quitar el cron y salir
    crontab -l 2>/dev/null | grep -v 'run_snapshot.sh' | crontab -
    exit 0
  fi
fi
.venv/bin/python scripts/diversity_snapshot.py >> logs/diversity_snapshot_cron.log 2>&1
