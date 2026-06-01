#!/usr/bin/env bash
set -uo pipefail
cd /opt/deal-agent/ofertas_hunter

echo "=== Eventos ML Session Recovery ==="
sqlite3 -readonly data/ofertas_hunter.db <<'SQL'
.mode line
SELECT substr(created_at,1,19) AS ts,
       kind,
       substr(payload_json,1,300) AS payload
FROM runtime_events
WHERE kind LIKE 'ml_session%'
   OR kind LIKE 'ml_cookies%'
ORDER BY id DESC
LIMIT 10;
SQL

echo
echo "=== Procesos activos del bot ==="
ps -eo pid,etime,cmd | grep -E 'orquestador_ia|kiro-cli' | grep -v grep | head -5

echo
echo "=== Ultimo cookie_expiry ==="
sqlite3 -readonly data/ofertas_hunter.db <<'SQL'
SELECT substr(created_at,1,19) AS ts, kind, substr(payload_json,1,200) AS p
FROM runtime_events
WHERE kind = 'cookie_expiry'
ORDER BY id DESC
LIMIT 3;
SQL
