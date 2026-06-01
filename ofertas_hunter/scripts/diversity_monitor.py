#!/usr/bin/env python3
"""Monitor read-only post-deploy de diversidad.

Muestra:
- últimas N decisiones del curator (con override/llm/fallback + rejected).
- publicaciones desde un timestamp dado (categoría/marca normalizada inferida).
- chequeo de hard caps: máx por categoría/marca en cualquier ventana de 10.
"""
import json
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, "src")
from ofertas_hunter.dispatching.diversity_metadata import (
    infer_offer_category, normalize_brand, product_family,
)

DB = "data/ofertas_hunter.db"
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-05-30T22:45:00Z"

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=15000")

print("===== Últimas 12 decisiones diversity_curator_decision =====")
rows = c.execute(
    "SELECT created_at, payload_json FROM runtime_events "
    "WHERE kind='diversity_curator_decision' ORDER BY id DESC LIMIT 12"
).fetchall()
for r in rows:
    try:
        p = json.loads(r["payload_json"])
    except Exception:
        continue
    # detectar esquema nuevo vs viejo
    new = "candidates_after_hard_caps" in p
    if new:
        print(f"  {r['created_at']} sel={p.get('selected_outbox_id')} "
              f"cat={p.get('selected_category_normalized')} brand={p.get('selected_brand_normalized')} "
              f"cand={p.get('candidates_count')}->{p.get('candidates_after_hard_caps')} "
              f"llm={p.get('llm_used')} fb={p.get('fallback_used')} ovr={p.get('override_used')} "
              f"rej={len(p.get('rejected_candidates') or [])}")
    else:
        print(f"  {r['created_at']} [OLD-SCHEMA] {r['payload_json'][:90]}")

print("\n===== Ejemplo rejected_candidates (decisión más reciente con rechazos) =====")
for r in rows:
    p = json.loads(r["payload_json"])
    rej = p.get("rejected_candidates") or []
    if rej:
        print(f"  decisión {r['created_at']} (seleccionó outbox={p.get('selected_outbox_id')}):")
        for x in rej[:8]:
            print(f"    - outbox={x['outbox_id']} reason={x['reason']} cat={x.get('category')} "
                  f"brand={x.get('brand')} fam={x.get('product_family')} | {x['title'][:50]}")
        break
else:
    print("  (aún no hay decisiones con rechazos)")

print(f"\n===== Publicaciones exitosas desde {SINCE} =====")
pub = c.execute(
    """
    SELECT pm.id pm_id, pm.outbox_id, pm.sent_at, o.message_payload_json, p.title p_title, p.brand p_brand
    FROM published_messages pm
    LEFT JOIN outbox o ON pm.outbox_id=o.id
    LEFT JOIN offers f ON f.id=pm.offer_id
    LEFT JOIN products p ON p.id=f.product_id
    WHERE pm.success=1 AND pm.sent_at >= ?
    ORDER BY pm.id ASC
    """,
    (SINCE,),
).fetchall()

feed = []
for r in pub:
    try:
        payload = json.loads(r["message_payload_json"]) if r["message_payload_json"] else {}
    except Exception:
        payload = {}
    title = payload.get("title") or r["p_title"] or ""
    mkt = payload.get("marketplace") or "?"
    cat = infer_offer_category(title, payload, mkt, payload.get("source")).normalized
    brand = normalize_brand(payload.get("brand") or r["p_brand"], title)
    fam = product_family(title, payload.get("brand") or r["p_brand"])
    feed.append((r["sent_at"], mkt, cat, brand, fam, title))

print(f"total nuevas: {len(feed)}")
for i, (ts, mkt, cat, brand, fam, title) in enumerate(feed, 1):
    print(f"  {i:>2} {ts[11:19]} {mkt[:4]:>4} {cat:<22} {str(brand)[:12]:<12} | {title[:46]}")

# chequeo hard caps en ventanas móviles de 10
def max_in_window(seq, w=10):
    best = Counter()
    for i in range(len(seq)):
        win = seq[max(0, i-w+1):i+1]
        cc = Counter(x for x in win if x)
        for k, v in cc.items():
            best[k] = max(best[k], v)
    return best

if feed:
    cats = [f[2] for f in feed]
    brands = [f[3] for f in feed]
    fams = [f[4] for f in feed]
    mc = max_in_window(cats); mb = max_in_window(brands); mf = max_in_window(fams)
    print("\n===== Chequeo hard caps (máximo por ventana móvil de 10) =====")
    print("  máx misma categoría:", mc.most_common(3), "(límite 3)")
    print("  máx misma marca:", mb.most_common(3), "(límite 2)")
    print("  máx misma familia:", mf.most_common(3), "(límite 1)")
    viol_cat = {k: v for k, v in mc.items() if v > 3}
    viol_brand = {k: v for k, v in mb.items() if v > 2}
    viol_fam = {k: v for k, v in mf.items() if v > 1}
    print("  VIOLACIONES categoría>3:", viol_cat or "ninguna")
    print("  VIOLACIONES marca>2:", viol_brand or "ninguna")
    print("  VIOLACIONES familia>1:", viol_fam or "ninguna (puede haber override si pool pobre)")
