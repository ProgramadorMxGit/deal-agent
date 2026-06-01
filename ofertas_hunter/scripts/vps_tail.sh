#!/usr/bin/env bash
# vps_tail.sh - Imprime un snapshot del estado del bot.
# Diseñado para ser invocado en loop desde el host (PowerShell, etc.).
# No usa watch: hace un solo pase y termina.

set -uo pipefail

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="$BOT_DIR/data/ofertas_hunter.db"
PYTHON="$BOT_DIR/.venv/bin/python"

color_cyan='\033[36m'
color_reset='\033[0m'

echo
echo -e "  ${color_cyan}=== STATUS BOT ===${color_reset}"
"$PYTHON" -m ofertas_hunter status 2>/dev/null | sed -n '/--- ofertas_hunter status ---/,$p' | head -45 || true

echo
echo -e "  ${color_cyan}=== ULTIMOS 12 RUNTIME_EVENTS ===${color_reset}"
sqlite3 -readonly "$DB" <<'SQL'
.mode column
.headers on
.width 19 8 30 90
SELECT substr(created_at, 1, 19)                       AS ts,
       severity                                        AS sev,
       kind                                            AS kind,
       substr(coalesce(payload_json, ''), 1, 90)       AS payload
FROM runtime_events
ORDER BY id DESC
LIMIT 12;
SQL

echo
echo -e "  ${color_cyan}=== ULTIMOS 12 OUTBOX (mas reciente arriba) ===${color_reset}"
sqlite3 -readonly "$DB" <<'SQL'
.mode column
.headers on
.width 5 13 13 9 4 50
SELECT o.id                                AS id,
       p.marketplace                       AS mkt,
       o.type                              AS type,
       o.state                             AS state,
       of.score                            AS sc,
       substr(p.title, 1, 50)              AS title
FROM outbox o
JOIN offers   of ON of.id = o.offer_id
JOIN products p  ON p.id  = of.product_id
ORDER BY o.id DESC
LIMIT 12;
SQL
echo
