import sqlite3, json
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB = Path("/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db")
c = sqlite3.connect(str(DB)); c.row_factory = sqlite3.Row

now = datetime.now(timezone.utc)
cutoff5 = (now - timedelta(minutes=5)).isoformat(timespec="milliseconds").replace("+00:00","Z")
cutoff30 = (now - timedelta(minutes=30)).isoformat(timespec="milliseconds").replace("+00:00","Z")

skip_5m = c.execute("SELECT COUNT(*) FROM runtime_events WHERE kind='ml_hunt_skipped_session_invalid' AND created_at>=?", (cutoff5,)).fetchone()[0]
print(f"ml_hunt_skipped_session_invalid en ultimos 5min: {skip_5m}")

last_pub = c.execute("SELECT id, sent_at FROM published_messages WHERE success=1 ORDER BY id DESC LIMIT 1").fetchone()
if last_pub:
    delta = now - datetime.fromisoformat(last_pub["sent_at"].replace("Z","+00:00"))
    print(f"Ultima publicacion exitosa: pm#{last_pub['id']} hace {int(delta.total_seconds()/60)} min")
else:
    print("Sin publicaciones exitosas")

last_fail = c.execute("SELECT id, sent_at FROM published_messages WHERE success=0 ORDER BY id DESC LIMIT 3").fetchall()
print(f"Ultimos 3 intentos fallidos:")
for r in last_fail:
    print(f"  pm#{r['id']} {r['sent_at']}")

pending = c.execute("SELECT COUNT(*) FROM outbox WHERE state='pending'").fetchone()[0]
print(f"Outbox pending: {pending}")

# Estado del manager ML
mgr_state = c.execute("SELECT payload_json, created_at FROM runtime_events WHERE kind='ml_session_state_changed' ORDER BY id DESC LIMIT 1").fetchone()
if mgr_state:
    p = json.loads(mgr_state["payload_json"])
    print(f"ML session manager: {p.get('current')} (reason={p.get('reason')}) at {mgr_state['created_at']}")

# Eventos de validacion activa
val = c.execute("SELECT payload_json, created_at FROM runtime_events WHERE kind='ml_session_active_validation' ORDER BY id DESC LIMIT 3").fetchall()
print(f"Ultimas validaciones activas:")
for r in val:
    p = json.loads(r["payload_json"])
    print(f"  phase={p.get('phase')} ok={p.get('ok')} reason={p.get('reason')} at {r['created_at']}")

# Eventos de dispatch recientes
disp = c.execute("SELECT kind, created_at FROM runtime_events WHERE kind LIKE '%dispatch%' OR kind LIKE '%publish%' OR kind='ml_cookies_reloaded' ORDER BY id DESC LIMIT 5").fetchall()
print(f"Eventos dispatch/publish recientes:")
for r in disp:
    print(f"  {r['kind']} {r['created_at']}")
