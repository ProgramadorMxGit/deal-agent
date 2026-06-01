"""Resetea el cursor del poller y el estado del ML manager.

Úsalo cuando el poller reprocesa mensajes viejos de cookies y el manager
se queda atascado en validating_received_cookies.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path("/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db")
if not DB.exists():
    DB = Path("data/ofertas_hunter.db")

c = sqlite3.connect(str(DB))
c.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

# 1) Borrar cursor del poller para que no reprocese mensajes viejos
r = c.execute("DELETE FROM runtime_events WHERE kind='ml_poller_cursor'")
print(f"Cursor del poller eliminado: {r.rowcount} filas")

# 2) Resetear estado del manager a 'valid'
c.execute(
    "INSERT INTO runtime_events (kind, severity, payload_json, created_at) VALUES (?,?,?,?)",
    (
        "ml_session_state_changed",
        "info",
        json.dumps({"previous": "validating_received_cookies", "current": "valid", "reason": "manual_reset_cursor"}),
        now,
    ),
)
print(f"Estado ML manager reseteado a 'valid'")

# 3) Limpiar spam de eventos de skip (últimas 2h)
cutoff = "2026-05-28T15:00:00.000Z"
r2 = c.execute(
    "DELETE FROM runtime_events WHERE kind='ml_hunt_skipped_session_invalid' AND created_at >= ?",
    (cutoff,)
)
print(f"Eventos de skip eliminados: {r2.rowcount}")

c.commit()
print("OK")
