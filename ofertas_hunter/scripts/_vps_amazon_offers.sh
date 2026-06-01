#!/usr/bin/env bash
set -uo pipefail
DB=/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db

echo "=== ULTIMAS OFERTAS AMAZON EN OUTBOX (mas recientes arriba) ==="
sqlite3 -readonly "$DB" <<'SQL'
.mode column
.headers on
.width 5 9 13 4 7 8 60
SELECT o.id                                    AS id,
       o.state                                 AS state,
       of.classification                       AS classifi,
       printf('%3d', of.score)                 AS sc,
       printf('%4.0f', coalesce(of.discount_percent,0)) AS disc,
       substr(o.enqueued_at, 12, 5)            AS hora,
       substr(p.title, 1, 60)                  AS title
FROM outbox o
JOIN offers   of ON of.id = o.offer_id
JOIN products p  ON p.id  = of.product_id
WHERE p.marketplace = 'amazon'
ORDER BY o.id DESC
LIMIT 25;
SQL

echo
echo "=== Detalles precio + URL afiliada de las 10 ultimas Amazon ==="
sqlite3 -readonly "$DB" <<'SQL'
.mode line
SELECT o.id AS id,
       o.state AS state,
       p.title AS title,
       json_extract(o.message_payload_json, '$.previous_price') AS prev_price,
       json_extract(o.message_payload_json, '$.current_price')  AS price,
       json_extract(o.message_payload_json, '$.discount_percent') AS disc,
       substr(json_extract(o.message_payload_json, '$.url'), 1, 90) AS url
FROM outbox o
JOIN offers   of ON of.id = o.offer_id
JOIN products p  ON p.id  = of.product_id
WHERE p.marketplace = 'amazon'
ORDER BY o.id DESC
LIMIT 10;
SQL

echo
echo "=== TOTALES AMAZON POR ESTADO + TIPO ==="
sqlite3 -readonly "$DB" <<'SQL'
.mode column
.headers on
SELECT o.state, o.type, COUNT(*) AS total
FROM outbox o
JOIN offers   of ON of.id = o.offer_id
JOIN products p  ON p.id  = of.product_id
WHERE p.marketplace = 'amazon'
GROUP BY o.state, o.type
ORDER BY total DESC;
SQL

echo
echo "=== AMAZON pending CON previous_price (publicables) ==="
sqlite3 -readonly "$DB" <<'SQL'
.mode column
.headers on
.width 5 6 60
SELECT o.id AS id,
       printf('%4.0f', coalesce(of.discount_percent,0)) AS disc,
       substr(p.title, 1, 60) AS title
FROM outbox o
JOIN offers   of ON of.id = o.offer_id
JOIN products p  ON p.id  = of.product_id
WHERE p.marketplace='amazon'
  AND o.state='pending'
  AND o.message_payload_json LIKE '%"previous_price"%'
  AND o.message_payload_json NOT LIKE '%"previous_price": null%'
ORDER BY o.id DESC;
SQL
