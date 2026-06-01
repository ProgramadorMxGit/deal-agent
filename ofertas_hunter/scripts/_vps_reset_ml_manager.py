"""Resetea el estado del ML session manager a 'valid' en la DB.

Úsalo cuando el manager se queda atascado en 'validating_received_cookies'
y el bot no puede publicar porque la DB está saturada de eventos de skip.
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

# Ver estado actual
last = c.execute(
    "SELECT payload_json, created_at FROM runtime_events "
    "WHERE kind='ml_session_state_changed' ORDER BY id DESC LIMIT 1"
).fetchone()
if last:
    p = json.loads(last["payload_json"])
    print(f"Estado actual: {p.get('current')} (reason={p.get('reason')}) at {last['created_at']}")
else:
    print("Sin estado registrado")

# Insertar transición a 'valid'
c.execute(
    "INSERT INTO runtime_events (kind, severity, payload_json, created_at) VALUES (?,?,?,?)",
    (
        "ml_session_state_changed",
        "info",
        json.dumps({"previous": "validating_received_cookies", "current": "valid", "reason": "manual_reset"}),
        now,
    ),
)
c.commit()
print(f"Estado reseteado a 'valid' en DB ({now})")
print()
print("NOTA: el proceso del bot en memoria sigue con el estado viejo hasta que")
print("reinicies el bot. Pero el spam de eventos se detendrá en el próximo ciclo")
print("porque el manager en memoria también necesita actualizarse.")
print()
print("Para que tome efecto inmediato, reinicia el bot:")
print("  kill <pid_orquestador_ia> && cd /opt/deal-agent/ofertas_hunter && ./start.sh")
