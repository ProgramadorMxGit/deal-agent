#!/usr/bin/env bash
set -uo pipefail
DB=/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db

echo "=== ULTIMAS OFERTAS ML EN OUTBOX (mas recientes arriba) ==="
sqlite3 -readonly "$DB" <<'SQL'
.mode column
.headers on
.width 5 9 4 7 8 60
SELECT o.id                                    AS id,
       o.state                                 AS state,
       printf('%3d', of.score)                 AS sc,
       printf('%4.0f', of.discount_percent)    AS disc,
       substr(o.enqueued_at, 12, 5)            AS hora,
       substr(p.title, 1, 60)                  AS title
FROM outbox o
JOIN offers   of ON of.id = o.offer_id
JOIN products p  ON p.id  = of.product_id
WHERE p.marketplace = 'mercadolibre'
ORDER BY o.id DESC
LIMIT 20;
SQL

echo
echo "=== Detalles precio + URL afiliado de las 10 mas recientes ==="
sqlite3 -readonly "$DB" <<'SQL'
.mode line
SELECT o.id AS id,
       o.state AS state,
       p.title AS title,
       json_extract(o.message_payload_json, '$.previous_price') AS prev_price,
       json_extract(o.message_payload_json, '$.current_price')  AS price,
       json_extract(o.message_payload_json, '$.discount_percent') AS disc,
       substr(json_extract(o.message_payload_json, '$.affiliate_url'), 1, 80) AS aff_url
FROM outbox o
JOIN offers   of ON of.id = o.offer_id
JOIN products p  ON p.id  = of.product_id
WHERE p.marketplace = 'mercadolibre'
ORDER BY o.id DESC
LIMIT 10;
SQL

echo
echo "=== TOTALES ML POR ESTADO ==="
sqlite3 -readonly "$DB" <<'SQL'
.mode column
.headers on
SELECT o.state, COUNT(*) AS total
FROM outbox o
JOIN offers   of ON of.id = o.offer_id
JOIN products p  ON p.id  = of.product_id
WHERE p.marketplace = 'mercadolibre'
GROUP BY o.state
ORDER BY total DESC;
SQL
