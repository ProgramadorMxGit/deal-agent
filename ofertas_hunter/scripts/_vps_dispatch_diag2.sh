#!/usr/bin/env bash
set -uo pipefail
DB=/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db

echo "=== Ultimos 6 dispatch_outbox AFTER (sin filtro phase, ordenado por id) ==="
sqlite3 -readonly "$DB" <<'SQL'
.mode line
SELECT substr(created_at,1,19) AS ts,
       substr(payload_json, 1, 800) AS payload
FROM runtime_events
WHERE payload_json LIKE '%dispatch_outbox%'
ORDER BY id DESC LIMIT 6;
SQL

echo
echo "=== Items pending por marketplace + 5 ultimos ML pendientes con detalles ==="
sqlite3 -readonly "$DB" <<'SQL'
.mode column
.headers on
.width 5 13 8 5 50
SELECT o.id, p.marketplace, of.classification, of.score, substr(p.title,1,50) AS title
FROM outbox o
JOIN offers   of ON of.id = o.offer_id
JOIN products p  ON p.id  = of.product_id
WHERE o.state='pending' AND p.marketplace='mercadolibre'
ORDER BY o.id DESC LIMIT 5;
SQL

echo
echo "=== Items state=failed (con razon de fallo si la registra) ==="
sqlite3 -readonly "$DB" <<'SQL'
.mode line
SELECT o.id, p.marketplace, o.attempts, o.last_attempt_at,
       substr(o.message_payload_json,1,400) AS payload
FROM outbox o
JOIN offers   of ON of.id = o.offer_id
JOIN products p  ON p.id  = of.product_id
WHERE o.state='failed'
ORDER BY o.id DESC LIMIT 5;
SQL

echo
echo "=== Ultimo published_at en outbox (dispatcher exitoso) ==="
sqlite3 -readonly "$DB" <<'SQL'
SELECT id, state, last_attempt_at, p.marketplace
FROM outbox o
JOIN offers   of ON of.id = o.offer_id
JOIN products p  ON p.id  = of.product_id
WHERE o.state='sent'
ORDER BY o.id DESC LIMIT 5;
SQL
