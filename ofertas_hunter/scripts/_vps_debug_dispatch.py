"""Debug: simula qué haría el dispatcher con los items pending actuales."""
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB = Path("/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db")
if not DB.exists():
    DB = Path("data/ofertas_hunter.db")

c = sqlite3.connect(str(DB)); c.row_factory = sqlite3.Row
now = datetime.now(timezone.utc)
now_iso = now.isoformat(timespec="milliseconds").replace("+00:00","Z")

# Cooldown: última publicación normal
last_pub = c.execute(
    "SELECT pm.sent_at FROM published_messages pm "
    "JOIN outbox o ON pm.outbox_id=o.id "
    "WHERE pm.success=1 AND o.type='normal' ORDER BY pm.id DESC LIMIT 1"
).fetchone()
cooldown_ok = True
cooldown_remaining = 0
if last_pub:
    delta = now - datetime.fromisoformat(last_pub["sent_at"].replace("Z","+00:00"))
    cooldown_remaining = max(0, 300 - int(delta.total_seconds()))
    cooldown_ok = delta.total_seconds() >= 300
    print(f"Cooldown: última pub hace {int(delta.total_seconds())}s, remaining={cooldown_remaining}s, ok={cooldown_ok}")
else:
    print("Cooldown: sin publicaciones previas, ok=True")

# Analizar top 20 pending
rows = c.execute(
    "SELECT id, type, enqueued_at, attempts, "
    "json_extract(message_payload_json,'$.title') title, "
    "json_extract(message_payload_json,'$.current_price') cur, "
    "json_extract(message_payload_json,'$.previous_price') prev, "
    "json_extract(message_payload_json,'$.discount_percent') disc, "
    "json_extract(message_payload_json,'$.item_id') iid, "
    "json_extract(message_payload_json,'$.asin') asin "
    "FROM outbox WHERE state='pending' ORDER BY id DESC LIMIT 20"
).fetchall()

print(f"\nTop 20 pending (total={c.execute('SELECT COUNT(*) FROM outbox WHERE state=?',('pending',)).fetchone()[0]}):")
stale_count = 0
dup_count = 0
ok_count = 0
for r in rows:
    enq = datetime.fromisoformat(r["enqueued_at"].replace("Z","+00:00"))
    age_h = (now - enq).total_seconds() / 3600
    iid = r["iid"] or r["asin"] or "?"

    # Stale check
    stale = r["type"] == "normal" and r["prev"] is not None and age_h > 4
    # Dup check
    dup_row = c.execute(
        "SELECT pm.id FROM published_messages pm JOIN outbox o ON pm.outbox_id=o.id "
        "WHERE pm.success=1 AND pm.sent_at >= ? AND pm.outbox_id != ? "
        "AND (json_extract(o.message_payload_json,'$.item_id')=? OR json_extract(o.message_payload_json,'$.asin')=?) LIMIT 1",
        ((now - timedelta(hours=48)).isoformat(timespec="milliseconds").replace("+00:00","Z"), r["id"], iid, iid)
    ).fetchone()
    dup = bool(dup_row)

    status = "STALE" if stale else ("DUP" if dup else "OK")
    if stale: stale_count += 1
    elif dup: dup_count += 1
    else: ok_count += 1

    print(f"  ob#{r['id']} [{r['type']}] age={age_h:.1f}h disc={r['disc']}% prev={r['prev']} → {status}")
    if status == "OK":
        print(f"    '{(r['title'] or '')[:60]}'")

print(f"\nResumen: OK={ok_count} STALE={stale_count} DUP={dup_count}")
print(f"Cooldown bloqueando: {not cooldown_ok} (remaining={cooldown_remaining}s)")
