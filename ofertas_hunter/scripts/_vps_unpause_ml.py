"""Emite evento manual de unpause ML después de un login fresco.

Uso (en VPS):
    cd /opt/deal-agent/ofertas_hunter
    ./.venv/bin/python scripts/_vps_unpause_ml.py
"""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

DB = Path("data/ofertas_hunter.db")
con = sqlite3.connect(str(DB))
con.row_factory = sqlite3.Row

now = datetime.now(timezone.utc).isoformat()

# 1) Registrar evento de unpause manual.
con.execute(
    "INSERT INTO runtime_events (kind, severity, payload_json, created_at) VALUES (?, ?, ?, ?)",
    (
        "mcp_marketplace_unpaused",
        "info",
        '{"marketplace": "mercadolibre", "reason": "manual_after_login"}',
        now,
    ),
)

# 2) Mostrar último estado de pause + cookies.
last_pause = con.execute(
    """
    SELECT created_at, payload_json
    FROM runtime_events
    WHERE kind = 'mcp_marketplace_paused'
    ORDER BY id DESC
    LIMIT 3
    """
).fetchall()

last_unpause = con.execute(
    """
    SELECT created_at, payload_json
    FROM runtime_events
    WHERE kind = 'mcp_marketplace_unpaused'
    ORDER BY id DESC
    LIMIT 3
    """
).fetchall()

con.commit()

print("=== Pauses recientes ===")
for r in last_pause:
    print(f"  {r['created_at']}  {r['payload_json'][:120]}")

print()
print("=== Unpauses recientes ===")
for r in last_unpause:
    print(f"  {r['created_at']}  {r['payload_json'][:120]}")

print()
print("OK: evento mcp_marketplace_unpaused emitido para mercadolibre.")
print("Cuando arranques el orquestador, ML ya no estará pausado.")
