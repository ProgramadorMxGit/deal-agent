"""¿Existen warnings/errors REALES de ML cookie expiry en las últimas 3h?"""
import sqlite3
from datetime import datetime, timedelta, timezone

conn = sqlite3.connect('data/ofertas_hunter.db')
conn.row_factory = sqlite3.Row

cutoff = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()

rows = list(conn.execute(
    "SELECT created_at, kind, severity FROM runtime_events "
    "WHERE created_at >= ? AND severity IN ('warning','error','critical') "
    "AND kind != 'mcp_marketplace_paused' "
    "ORDER BY id DESC",
    (cutoff,),
))

print(f"Warnings/errors reales (excluyendo mcp_marketplace_paused) últimas 3h: {len(rows)}")
for r in rows:
    print(f"  {r['created_at']} | {r['kind']} | {r['severity']}")

# También: hunts ML reales
print()
print("=== hunt_mercadolibre invocaciones últimas 30 min ===")
cutoff2 = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
for r in conn.execute(
    "SELECT created_at, payload_json FROM runtime_events "
    "WHERE kind='mcp_tool_called' AND created_at >= ? "
    "AND payload_json LIKE '%hunt_mercadolibre%' "
    "AND payload_json LIKE '%phase\":\"after%' "
    "ORDER BY id DESC LIMIT 5",
    (cutoff2,),
):
    print(r['created_at'])
    print(' ', r['payload_json'][:300])
