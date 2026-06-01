#!/usr/bin/env bash
# Respalda .env, DB, secrets/, logs/ en backups/<timestamp>/
set -euo pipefail
cd /opt/deal-agent/ofertas_hunter
TS=$(date +%Y%m%d_%H%M%S)
DEST=backups/$TS
mkdir -p "$DEST"
[ -f .env ] && cp .env "$DEST/.env"
[ -f data/ofertas_hunter.db ] && cp data/ofertas_hunter.db "$DEST/ofertas_hunter.db"
[ -d secrets ] && tar -czf "$DEST/secrets.tar.gz" secrets/
[ -d logs ] && tar -czf "$DEST/logs.tar.gz" logs/ 2>/dev/null || true
echo "Backup OK: $DEST"
ls -lh "$DEST"
# Mantener últimos 14 backups
ls -1tdr backups/*/ 2>/dev/null | head -n -14 | xargs -r rm -rf
