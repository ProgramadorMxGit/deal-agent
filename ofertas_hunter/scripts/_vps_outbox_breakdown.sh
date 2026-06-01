#!/usr/bin/env bash
set -uo pipefail
DB=/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db

echo "=== Pendientes con/sin previous_price y discount_percent ==="
sqlite3 -readonly "$DB" <<'SQL'
WITH pending AS (
  SELECT o.id, o.message_payload_json, p.marketplace, of.score, p.title
  FROM outbox o
  JOIN offers   of ON of.id = o.offer_id
  JOIN products p  ON p.id  = of.product_id
  WHERE o.state='pending'
)
SELECT
  marketplace,
  COUNT(*) AS total,
  SUM(CASE WHEN message_payload_json LIKE '%"previous_price"%' AND message_payload_json NOT LIKE '%"previous_price": null%' THEN 1 ELSE 0 END) AS con_prev,
  SUM(CASE WHEN message_payload_json LIKE '%"discount_percent"%' AND message_payload_json NOT LIKE '%"discount_percent": null%' THEN 1 ELSE 0 END) AS con_disc,
  SUM(CASE WHEN message_payload_json LIKE '%"previous_price"%' AND message_payload_json NOT LIKE '%"previous_price": null%'
            AND message_payload_json LIKE '%"discount_percent"%' AND message_payload_json NOT LIKE '%"discount_percent": null%' THEN 1 ELSE 0 END) AS publicables
FROM pending
GROUP BY marketplace;
SQL

echo
echo "=== 5 items publicables (con prev_price + discount) ==="
sqlite3 -readonly "$DB" <<'SQL'
.mode column
.headers on
.width 5 13 4 50
SELECT o.id, p.marketplace, of.score, substr(p.title,1,50) AS title
FROM outbox o
JOIN offers   of ON of.id = o.offer_id
JOIN products p  ON p.id  = of.product_id
WHERE o.state='pending'
  AND o.message_payload_json LIKE '%"previous_price"%'
  AND o.message_payload_json NOT LIKE '%"previous_price": null%'
  AND o.message_payload_json LIKE '%"discount_percent"%'
  AND o.message_payload_json NOT LIKE '%"discount_percent": null%'
ORDER BY o.id DESC LIMIT 10;
SQL
