"""Diagnóstico: por qué el dispatcher no está mandando ofertas al grupo."""
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB = Path("/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db")
if not DB.exists():
    DB = Path("data/ofertas_hunter.db")

c = sqlite3.connect(str(DB))
c.row_factory = sqlite3.Row

now_dt = datetime.now(timezone.utc)
now_iso = now_dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
print(f"now: {now_iso}")
print()

# 1) Última publicación exitosa
last = c.execute(
    "SELECT id, sent_at, success FROM published_messages "
    "WHERE success=1 ORDER BY id DESC LIMIT 1"
).fetchone()
if last:
    delta = now_dt - datetime.fromisoformat(last["sent_at"].replace("Z", "+00:00"))
    print(f"Última publicación EXITOSA: pm#{last['id']} hace {int(delta.total_seconds()/60)} min ({last['sent_at']})")
else:
    print("Sin publicaciones exitosas")
print()

# 2) Últimos 10 intentos (success o failure)
print("=== Últimos 10 intentos de publicación ===")
recent = c.execute(
    "SELECT id, outbox_id, sent_at, success, message_text "
    "FROM published_messages ORDER BY id DESC LIMIT 10"
).fetchall()
for r in recent:
    txt = (r["message_text"] or "")[:50].replace("\n", " | ")
    print(f"  pm#{r['id']} ob#{r['outbox_id']} ok={r['success']} {r['sent_at']}  '{txt}'")
print()

# 3) Estado del outbox
print("=== Estado outbox ===")
states = c.execute(
    "SELECT type, state, COUNT(*) c FROM outbox GROUP BY type, state ORDER BY type, state"
).fetchall()
for r in states:
    print(f"  {r['type']:<15} {r['state']:<12} {r['c']}")
print()

# 4) Items pending más recientes (los que el dispatcher debería procesar)
print("=== Top 10 outbox PENDING más recientes ===")
pending = c.execute(
    "SELECT id, type, enqueued_at, scheduled_for, attempts, last_attempt_at, "
    "json_extract(message_payload_json, '$.title') title, "
    "json_extract(message_payload_json, '$.discount_percent') disc "
    "FROM outbox WHERE state='pending' ORDER BY id DESC LIMIT 10"
).fetchall()
for r in pending:
    print(f"  ob#{r['id']} type={r['type']:<13} attempts={r['attempts']} disc={r['disc']}%")
    print(f"     enqueued={r['enqueued_at']}  last_attempt={r['last_attempt_at']}")
    print(f"     '{(r['title'] or '')[:60]}'")
print()

# 5) Eventos recientes con severity warning/error/critical
print("=== Últimos 20 runtime_events warning/error/critical ===")
ev = c.execute(
    "SELECT id, kind, severity, substr(payload_json,1,160) p, created_at "
    "FROM runtime_events WHERE severity IN ('warning','error','critical') "
    "ORDER BY id DESC LIMIT 20"
).fetchall()
for r in ev:
    print(f"  #{r['id']} {r['created_at']} [{r['severity']}] {r['kind']}")
    print(f"    {r['p']}")
print()

# 6) Cooldown info
print("=== Cooldown ===")
last_normal = c.execute(
    "SELECT pm.id, pm.sent_at FROM published_messages pm "
    "JOIN outbox o ON pm.outbox_id=o.id "
    "WHERE pm.success=1 AND o.type='normal' "
    "ORDER BY pm.id DESC LIMIT 1"
).fetchone()
if last_normal:
    delta = now_dt - datetime.fromisoformat(last_normal["sent_at"].replace("Z", "+00:00"))
    print(f"Última 'normal' publicada: pm#{last_normal['id']} hace {int(delta.total_seconds()/60)} min")
    print(f"Cooldown 5 min: {'OK (puede publicar)' if delta.total_seconds() >= 300 else 'BLOQUEANDO'}")
