#!/usr/bin/env python3
"""¿Amazon hunter propio captura old_price_verified? vs telegram (READ-ONLY)."""
import json, sqlite3
from collections import Counter
DB = "data/ofertas_hunter.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=30000")

print("=== outbox Amazon por source + old_price_verified ===")
rows = c.execute("SELECT message_payload_json, state FROM outbox WHERE message_payload_json LIKE '%amazon%'").fetchall()
by_source = Counter(); verified_by_source = Counter(); sent_by_source = Counter()
for r in rows:
    try: p = json.loads(r["message_payload_json"])
    except Exception: continue
    if (p.get("marketplace") or "").lower() != "amazon": continue
    src = p.get("source") or "?"
    by_source[src] += 1
    if p.get("old_price_verified"):
        verified_by_source[src] += 1
    if r["state"] == "sent":
        sent_by_source[src] += 1
print("  total Amazon outbox por source:", dict(by_source))
print("  con old_price_verified=True por source:", dict(verified_by_source))
print("  sent por source:", dict(sent_by_source))

print("\n=== ¿Cuántos productos Amazon tienen source=amazon_hunter (crawl propio)? ===")
rows = c.execute("SELECT message_payload_json FROM outbox WHERE message_payload_json LIKE '%amazon_hunter%' LIMIT 5").fetchall()
print(f"  muestra de {len(rows)} (amazon_hunter):")
for r in rows:
    try: p = json.loads(r["message_payload_json"])
    except Exception: continue
    print(f"     old_verif={p.get('old_price_verified')} disc={p.get('discount_percent')} prev={p.get('previous_price')} | {(p.get('title') or '')[:42]}")

print("\n=== frontier Amazon: tipos de URL ===")
for r in c.execute("SELECT url_type, COUNT(*) n FROM frontier WHERE marketplace='amazon' GROUP BY url_type"):
    print(f"  {r['url_type']}: {r['n']}")
print("  sample amazon frontier URLs:")
for r in c.execute("SELECT url_canonical FROM frontier WHERE marketplace='amazon' LIMIT 12"):
    print("   ", r["url_canonical"][:90])
