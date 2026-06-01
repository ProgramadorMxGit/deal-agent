"""Diag rápido del recovery ML: imprime los últimos eventos relevantes."""
import sqlite3, sys
from pathlib import Path

DB = Path("/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db")
if not DB.exists():
    DB = Path("data/ofertas_hunter.db")

KINDS = (
    "cookie_expiry",
    "ml_session_admin_alerted",
    "ml_session_alert_skipped_cooldown",
    "ml_session_state_changed",
    "ml_cookies_received",
    "ml_cookies_validated",
    "ml_cookies_promoted",
    "ml_session_context_rotated",
    "ml_cookies_reloaded",
    "ml_session_inbound_no_command_ignored",
    "ml_session_inbound_unauthorized",
    "ml_session_inbound_non_admin_ignored",
    "ml_session_active_validation",
)

c = sqlite3.connect(str(DB))
c.row_factory = sqlite3.Row

q = (
    "SELECT id, kind, severity, substr(payload_json,1,200) p, created_at "
    "FROM runtime_events WHERE kind IN ({}) "
    "ORDER BY id DESC LIMIT 30".format(",".join("?" * len(KINDS)))
)
rows = c.execute(q, KINDS).fetchall()
if not rows:
    print("(sin eventos de recovery aún)")
else:
    for r in rows:
        print(f"#{r['id']:>5} {r['created_at']} {r['kind']:<40} {r['severity']:<8} {r['p']}")

# Última alerta vs último expiry
last_expiry = c.execute(
    "SELECT id, created_at FROM runtime_events WHERE kind='cookie_expiry' "
    "ORDER BY id DESC LIMIT 1"
).fetchone()
last_alert = c.execute(
    "SELECT id, created_at FROM runtime_events WHERE kind='ml_session_admin_alerted' "
    "ORDER BY id DESC LIMIT 1"
).fetchone()
print()
print("Resumen:")
print(f"  cookie_expiry último: {dict(last_expiry) if last_expiry else None}")
print(f"  ml_session_admin_alerted último: {dict(last_alert) if last_alert else None}")
