import sqlite3, json
c = sqlite3.connect("file:data/ofertas_hunter.db?mode=ro", uri=True, timeout=20)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=20000")
rows = c.execute(
    "SELECT created_at, payload_json FROM runtime_events WHERE kind='diversity_curator_decision' "
    "ORDER BY id DESC LIMIT 6"
).fetchall()
for r in rows:
    p = json.loads(r["payload_json"])
    if "candidates_after_hard_caps" not in p:
        continue
    print(f"\n@ {r['created_at']}")
    print(f"  selected: outbox={p.get('selected_outbox_id')} cat={p.get('selected_category_normalized')} "
          f"brand={p.get('selected_brand_normalized')} fam={p.get('selected_product_family')}")
    print(f"  candidates={p.get('candidates_count')} after_hard_caps={p.get('candidates_after_hard_caps')} "
          f"override={p.get('override_used')} reason={p.get('override_reason')} llm={p.get('llm_used')} fb={p.get('fallback_used')}")
    ws = p.get("window_stats") or {}
    print(f"  window_stats.category_counts={ws.get('category_counts')}")
    print(f"  window_stats.brand_counts={ws.get('brand_counts')}")
    rej = p.get("rejected_candidates") or []
    rc = {}
    for x in rej:
        rc[x.get("reason")] = rc.get(x.get("reason"), 0) + 1
    print(f"  rejected_total={len(rej)} by_reason={rc}")
    for x in rej[:4]:
        print(f"    - {x.get('reason')} brand={x.get('brand')} fam={x.get('product_family')} | {x.get('title','')[:40]}")
