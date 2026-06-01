#!/usr/bin/env bash
set -uo pipefail
DB=/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db

echo "=== Ultimos 5 dispatch_outbox AFTER (resultado) ==="
sqlite3 -readonly "$DB" <<'SQL'
.mode line
SELECT substr(created_at,1,19) AS ts,
       substr(payload_json, 1, 500) AS payload
FROM runtime_events
WHERE kind='mcp_tool_called'
  AND payload_json LIKE '%"phase":"after"%'
  AND payload_json LIKE '%"tool":"dispatch_outbox"%'
ORDER BY id DESC LIMIT 5;
SQL

echo
echo "=== Ultimos 10 events de cualquier kind relacionados a publish/dispatch/cooldown ==="
sqlite3 -readonly "$DB" <<'SQL'
.mode line
SELECT substr(created_at,1,19) AS ts,
       severity AS sev,
       kind AS kind,
       substr(payload_json, 1, 400) AS payload
FROM runtime_events
WHERE kind LIKE '%publish%'
   OR kind LIKE '%dispatch%'
   OR kind LIKE '%cooldown%'
   OR kind LIKE '%discard%'
ORDER BY id DESC LIMIT 10;
SQL

echo
echo "=== Items pending: cuantos pasarian por gates duros (image+price+url) ==="
sqlite3 -readonly "$DB" <<'SQL'
.mode column
.headers on
.width 5 13 50 8 8 5
SELECT o.id,
       p.marketplace AS mkt,
       substr(p.title, 1, 50) AS title,
       CASE WHEN p.image_url IS NULL OR p.image_url='' THEN 'NO' ELSE 'OK' END AS img,
       CASE WHEN p.affiliate_link IS NULL OR p.affiliate_link='' THEN 'NO_AFF' ELSE 'OK' END AS aff,
       of.score AS sc
FROM outbox o
JOIN offers   of ON of.id = o.offer_id
JOIN products p  ON p.id  = of.product_id
WHERE o.state = 'pending'
ORDER BY o.id DESC
LIMIT 15;
SQL
