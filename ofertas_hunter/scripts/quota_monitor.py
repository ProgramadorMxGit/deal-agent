#!/usr/bin/env python3
"""Monitor READ-ONLY post-deploy de cuotas/planner/deferred."""
import json, sqlite3, sys
from collections import Counter
sys.path.insert(0, "src")
from ofertas_hunter.dispatching.diversity_metadata import infer_offer_category, normalize_brand

DB = "data/ofertas_hunter.db"
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-05-31T01:16:40Z"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=25)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=25000")

print(f"=== runtime_events nuevos desde {SINCE} ===")
for r in c.execute("SELECT kind, COUNT(*) n FROM runtime_events WHERE created_at>=? GROUP BY kind ORDER BY n DESC", (SINCE,)):
    print(f"  {r['n']:>5}  {r['kind']}")

print("\n=== outbox por estado ===")
for r in c.execute("SELECT state, COUNT(*) n FROM outbox GROUP BY state ORDER BY n DESC"):
    print(f"  {r['n']:>5}  {r['state']}")

def snap(state):
    rows = c.execute("SELECT message_payload_json FROM outbox WHERE state=?", (state,)).fetchall()
    cat=Counter(); brand=Counter(); mkt=Counter()
    for r in rows:
        try: p=json.loads(r["message_payload_json"])
        except Exception: p={}
        t=p.get("title") or ""
        cat[infer_offer_category(t,p,p.get("marketplace"),p.get("source")).normalized]+=1
        brand[normalize_brand(p.get("brand"),t) or "-"]+=1
        mkt[p.get("marketplace") or "?"]+=1
    n=len(rows) or 1
    print(f"\n=== {state} (n={len(rows)}) ===")
    print("  cat:", {k:f"{v} ({round(100*v/n)}%)" for k,v in cat.most_common(8)})
    print("  brand:", dict(brand.most_common(6)))
    print("  mkt:", {k:f"{v} ({round(100*v/n)}%)" for k,v in mkt.most_common()})
snap("pending")
snap("deferred")

print("\n=== ultimo pool_deficit_plan ===")
r = c.execute("SELECT created_at, payload_json FROM runtime_events WHERE kind='pool_deficit_plan' ORDER BY id DESC LIMIT 1").fetchone()
if r:
    p = json.loads(r["payload_json"])
    print(" ", r["created_at"])
    print("  saturated_categories:", p.get("saturated_categories"))
    print("  deficit_categories:", p.get("deficit_categories"))
    print("  recommended_frontier:", p.get("recommended_frontier_categories"))
    print("  saturated_brands:", p.get("saturated_brands"))
else:
    print("  (ninguno aun)")

print("\n=== publicaciones desde deploy ===")
rows = c.execute("SELECT pm.sent_at, o.message_payload_json FROM published_messages pm LEFT JOIN outbox o ON pm.outbox_id=o.id WHERE pm.success=1 AND pm.sent_at>=? ORDER BY pm.id ASC", (SINCE,)).fetchall()
print(f"  total: {len(rows)}")
for r in rows:
    try: p=json.loads(r["message_payload_json"]) if r["message_payload_json"] else {}
    except Exception: p={}
    t=p.get("title") or ""
    cat=infer_offer_category(t,p,p.get("marketplace"),p.get("source")).normalized
    print(f"  {r['sent_at'][11:19]} {(p.get('marketplace') or '?')[:4]:>4} disc={p.get('discount_percent')} {cat:<20} {t[:38]}")
