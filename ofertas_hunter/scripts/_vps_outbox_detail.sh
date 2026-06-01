#!/usr/bin/env bash
set -uo pipefail
DB=/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db
IDS="$*"
[ -z "$IDS" ] && { echo "Uso: $0 <id1> <id2> ..."; exit 1; }

PLACEHOLDERS=$(echo "$IDS" | sed 's/[^0-9 ]//g' | tr ' ' ',')

sqlite3 -readonly "$DB" <<SQL
.mode line
SELECT o.id, o.state, of.classification, of.score,
       p.title,
       json_extract(o.message_payload_json, '$.previous_price')  AS prev_price,
       json_extract(o.message_payload_json, '$.current_price')   AS price,
       json_extract(o.message_payload_json, '$.discount_percent') AS disc,
       substr(json_extract(o.message_payload_json, '$.url'), 1, 100) AS url,
       substr(json_extract(o.message_payload_json, '$.image_url'), 1, 80) AS image
FROM outbox o
JOIN offers   of ON of.id = o.offer_id
JOIN products p  ON p.id  = of.product_id
WHERE o.id IN ($PLACEHOLDERS);
SQL
