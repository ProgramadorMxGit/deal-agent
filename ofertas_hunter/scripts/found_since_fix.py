#!/usr/bin/env python3
"""Lista la diversidad de productos ENCONTRADOS por el bot desde el deploy (READ-ONLY).

Incluye items enqueued al outbox (cualquier estado) DESPUÉS del deploy de cuotas,
agrupados por categoría, con marca/descuento/precio/marketplace.
"""
import json, sqlite3, sys
from collections import Counter, defaultdict
sys.path.insert(0, "src")
from ofertas_hunter.dispatching.diversity_metadata import infer_offer_category, normalize_brand

DB = "data/ofertas_hunter.db"
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-05-31T01:16:40Z"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=30000")

rows = c.execute(
    "SELECT id, state, enqueued_at, message_payload_json FROM outbox "
    "WHERE enqueued_at >= ? ORDER BY enqueued_at ASC", (SINCE,)
).fetchall()

by_cat = defaultdict(list)
cat_count = Counter()
for r in rows:
    try:
        p = json.loads(r["message_payload_json"])
    except Exception:
        continue
    title = p.get("title") or ""
    cat = infer_offer_category(title, p, p.get("marketplace"), p.get("source")).normalized
    brand = normalize_brand(p.get("brand"), title) or "-"
    disc = p.get("discount_percent")
    price = p.get("current_price")
    mkt = (p.get("marketplace") or "?")
    cat_count[cat] += 1
    by_cat[cat].append({
        "title": title, "brand": brand, "disc": disc, "price": price,
        "mkt": mkt, "state": r["state"],
    })

total = len(rows)
print(f"### Productos ENCONTRADOS por el bot desde {SINCE}")
print(f"### total items encolados al outbox: {total}")
print(f"### categorias distintas: {len(cat_count)}\n")
print("=== resumen por categoria ===")
for cat, n in cat_count.most_common():
    print(f"  {n:>3} ({round(100*n/total)}%)  {cat}")

print("\n=== DETALLE por categoria (productos distintos por titulo) ===")
for cat, n in cat_count.most_common():
    print(f"\n--- {cat} ({n}) ---")
    seen = set()
    for it in by_cat[cat]:
        key = it["title"][:50].lower()
        if key in seen:
            continue
        seen.add(key)
        ds = f"{it['disc']:.0f}%" if isinstance(it['disc'], (int, float)) else "-"
        pr = f"${it['price']:,.0f}" if isinstance(it['price'], (int, float)) else "-"
        print(f"   [{it['mkt'][:4]}] {ds:>4} {pr:>9} {str(it['brand'])[:14]:<15} {it['title'][:60]}")
