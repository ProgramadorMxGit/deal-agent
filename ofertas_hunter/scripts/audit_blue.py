#!/usr/bin/env python3
"""Auditoría READ-ONLY del falso positivo Blu-e (link meli.la/32TDYSu)."""
import json, sqlite3
DB = "data/ofertas_hunter.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=30000")

# Buscar por link o por titulo Blu-e
rows = c.execute(
    """
    SELECT pm.id pm_id, pm.outbox_id, pm.offer_id, pm.sent_at, pm.message_text,
           o.state ostate, o.type otype, o.message_payload_json,
           p.id product_id, p.title, p.url_canonical, p.brand, p.category
    FROM published_messages pm
    LEFT JOIN outbox o ON pm.outbox_id=o.id
    LEFT JOIN offers f ON f.id=pm.offer_id
    LEFT JOIN products p ON p.id=f.product_id
    WHERE pm.success=1 AND (
        pm.message_text LIKE '%Blu-e%' OR pm.message_text LIKE '%Blu e%'
        OR pm.message_text LIKE '%32TDYSu%' OR p.title LIKE '%Blu-e%'
        OR o.message_payload_json LIKE '%32TDYSu%'
    )
    ORDER BY pm.id DESC LIMIT 10
    """
).fetchall()
print(f"=== published Blu-e matches: {len(rows)} ===")
for r in rows:
    print(f"\npm_id={r['pm_id']} outbox_id={r['outbox_id']} offer_id={r['offer_id']} product_id={r['product_id']}")
    print(f"  sent_at={r['sent_at']} ostate={r['ostate']}")
    print(f"  title={r['title']}")
    print(f"  url_canonical={r['url_canonical']}")
    try:
        p = json.loads(r["message_payload_json"]) if r["message_payload_json"] else {}
    except Exception:
        p = {}
    for k in ("current_price","previous_price","discount_percent","item_id","affiliate_url",
              "url","canonical_url","marketplace","brand","category","source",
              "score","score_classification","confidence_label","requires_live_validation",
              "revalidated_at","old_price_verified","current_price_verified","previous_price_source",
              "current_price_source"):
        if k in p:
            print(f"  payload.{k} = {p.get(k)!r}")
    print("  ALL payload keys:", sorted(p.keys()))
    print("  --- message_text ---")
    print("  " + (r["message_text"] or "")[:500].replace("\n", "\n  "))

# price_observations del producto
if rows:
    pid = rows[0]["product_id"]
    if pid:
        print(f"\n=== price_observations para product_id={pid} ===")
        for po in c.execute(
            "SELECT id, source, current_price, previous_price, discount_percent, raw_signals, observed_at "
            "FROM price_observations WHERE product_id=? ORDER BY id DESC LIMIT 10", (pid,)
        ):
            print(f"  obs {po['id']} src={po['source']} cur={po['current_price']} prev={po['previous_price']} "
                  f"disc={po['discount_percent']} at={po['observed_at']}")
            if po["raw_signals"]:
                print(f"     raw_signals={po['raw_signals'][:400]}")
