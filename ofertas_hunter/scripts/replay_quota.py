#!/usr/bin/env python3
"""Replay READ-ONLY: simula las cuotas contra el pending real actual.

Para cada item pending actual, simula en qué orden habría llegado y si con
cuotas habría entrado a pending o ido a deferred. NO modifica nada.
"""
import json, sqlite3, sys
from collections import Counter
sys.path.insert(0, "src")
from ofertas_hunter.dispatching.outbox_admission import (
    QuotaConfig, decide_admission, PendingSnapshot,
)
from ofertas_hunter.dispatching.diversity_metadata import infer_offer_category, normalize_brand

DB = "data/ofertas_hunter.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=30000")

cfg = QuotaConfig(enabled=True, max_category_pct=40, max_brand_pct=20,
                  max_marketplace_pct=75, exceptional_discount_threshold=70,
                  min_pending_before_quota=8)

rows = c.execute(
    "SELECT id, message_payload_json FROM outbox WHERE state='pending' ORDER BY id ASC"
).fetchall()

# Simular llegada incremental: empezamos pending vacío y vamos admitiendo.
sim = PendingSnapshot(total=0, category_counts={}, brand_counts={}, marketplace_counts={})
results = []
pending_in = Counter(); deferred_in = Counter()
for r in rows:
    try:
        p = json.loads(r["message_payload_json"])
    except Exception:
        continue
    title = p.get("title") or ""
    cat = infer_offer_category(title, p, p.get("marketplace"), p.get("source")).normalized
    brand = normalize_brand(p.get("brand"), title) or "-"
    mkt = p.get("marketplace") or "unknown"
    disc = p.get("discount_percent")
    # decision usando snapshot simulado en memoria
    d = decide_admission(c, p, cfg, snapshot=sim, emit_event=False)
    action = "pending" if d.state == "pending" else f"deferred ({d.defer_reason})"
    results.append((r["id"], cat, brand, mkt, disc, action))
    if d.state == "pending":
        sim.category_counts[cat] = sim.category_counts.get(cat, 0) + 1
        sim.brand_counts[brand] = sim.brand_counts.get(brand, 0) + 1
        sim.marketplace_counts[mkt] = sim.marketplace_counts.get(mkt, 0) + 1
        sim.total += 1
        pending_in[cat] += 1
    else:
        deferred_in[cat] += 1

print(f"{'id':>5} {'cat':<20} {'brand':<12} {'mkt':>5} {'disc':>5}  accion")
for rid, cat, brand, mkt, disc, action in results:
    ds = f"{disc:.0f}" if isinstance(disc,(int,float)) else "-"
    print(f"{rid:>5} {cat[:19]:<20} {str(brand)[:11]:<12} {mkt[:5]:>5} {ds:>5}  {action}")

print(f"\n=== RESULTADO SIMULADO ===")
print(f"items totales pending real: {len(rows)}")
print(f"entrarian a PENDING: {sum(pending_in.values())} -> {dict(pending_in)}")
print(f"irian a DEFERRED: {sum(deferred_in.values())} -> {dict(deferred_in)}")
tot = sim.total or 1
print(f"pending simulado por categoria: " + ", ".join(f"{k}={v} ({round(100*v/tot)}%)" for k,v in sim.category_counts.items()))
print(f"pending simulado por marca: " + ", ".join(f"{k}={v}" for k,v in sorted(sim.brand_counts.items(), key=lambda x:-x[1])[:6]))
print(f"pending simulado por marketplace: " + ", ".join(f"{k}={v} ({round(100*v/tot)}%)" for k,v in sim.marketplace_counts.items()))
