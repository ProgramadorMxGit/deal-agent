import sqlite3, json
c = sqlite3.connect("file:data/ofertas_hunter.db?mode=ro", uri=True, timeout=20)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=20000")
print("amazon discovery snapshots por reason:")
for r in c.execute("SELECT reason, COUNT(*) n FROM dom_snapshots WHERE marketplace='amazon' GROUP BY reason ORDER BY n DESC"):
    print(" ", r["reason"], r["n"])
n = 0
for r in c.execute("SELECT message_payload_json FROM outbox WHERE state='sent' ORDER BY id DESC LIMIT 300"):
    try: p = json.loads(r["message_payload_json"])
    except Exception: continue
    if p.get("source") == "amazon_hunter":
        n += 1
print("amazon_hunter sent en ult 300 sent:", n)
# productos amazon recientes por fuente
from collections import Counter
src = Counter()
for r in c.execute("SELECT message_payload_json FROM outbox WHERE enqueued_at >= '2026-05-31T00:00:00Z' AND message_payload_json LIKE '%amazon%'"):
    try: p = json.loads(r["message_payload_json"])
    except Exception: continue
    if (p.get("marketplace") or "")=="amazon":
        src[p.get("source") or "?"] += 1
print("amazon encolado hoy por source:", dict(src))
