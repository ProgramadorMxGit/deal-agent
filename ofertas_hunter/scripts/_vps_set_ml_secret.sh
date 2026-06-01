#!/usr/bin/env bash
set -euo pipefail
cd /opt/deal-agent/ofertas_hunter

SECRET=$(./.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))")
# Reemplaza la línea vacía del secret
sed -i "s|^ML_SESSION_INBOUND_SECRET=.*|ML_SESSION_INBOUND_SECRET=${SECRET}|" .env
echo "Secret generado y guardado OK"
grep '^ML_SESSION' .env | sed 's/\(ML_SESSION_INBOUND_SECRET=\).*/\1***HIDDEN***/'
