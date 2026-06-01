#!/usr/bin/env bash
set -uo pipefail
cd /opt/deal-agent/ofertas_hunter

echo "=== Cookies file (timestamp + size) ==="
ls -la secrets/browser_profiles/mercadolibre/Default/Cookies 2>/dev/null
ls -la secrets/browser_profiles/mercadolibre/ | head -10

echo
echo "=== Eventos pause / cookie_expiry recientes ==="
sqlite3 -readonly data/ofertas_hunter.db <<'SQL'
.mode line
SELECT substr(created_at, 1, 19) AS ts,
       severity,
       kind,
       substr(payload_json, 1, 250) AS payload
FROM runtime_events
WHERE kind LIKE '%pause%'
   OR kind LIKE '%cookie%'
   OR kind LIKE '%ml_paused%'
ORDER BY id DESC
LIMIT 10;
SQL

echo
echo "=== Procesos activos del bot ==="
ps -eo pid,etime,cmd | grep -E 'orquestador_ia|ofertas_hunter' | grep -v grep | head -5

echo
echo "=== Pause state activos en runtime_events (último por marketplace) ==="
sqlite3 -readonly data/ofertas_hunter.db <<'SQL'
SELECT substr(created_at, 1, 19) AS ts,
       json_extract(payload_json, '$.marketplace') AS mkt,
       json_extract(payload_json, '$.until') AS until_ts,
       json_extract(payload_json, '$.reason') AS reason
FROM runtime_events
WHERE kind = 'mcp_marketplace_paused'
ORDER BY id DESC
LIMIT 5;
SQL
