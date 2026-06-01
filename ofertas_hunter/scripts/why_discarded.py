#!/usr/bin/env python3
"""Por qué se descartaron los benchmark Telegram-sourced (READ-ONLY)."""
import json, sqlite3
DB = "data/ofertas_hunter.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=30000")

for oid in (2871, 2863, 2059, 2039, 2034):
    r = c.execute("SELECT id, state, type, offer_id, message_payload_json FROM outbox WHERE id=?", (oid,)).fetchone()
    if not r:
        print(f"outbox {oid}: NOT FOUND"); continue
    try: p = json.loads(r["message_payload_json"])
    except Exception: p = {}
    print(f"\noutbox={oid} state={r['state']} type={r['type']} offer_id={r['offer_id']}")
    print(f"  title: {(p.get('title') or '')[:60]}")
    for k in ("marketplace","source","current_price","previous_price","discount_percent",
              "affiliate_url","discarded_reason","old_price_verified","current_price_verified",
              "ml_previous_price_verified","asin","item_id","url"):
        if k in p:
            print(f"  {k}={p.get(k)!r}")

print("\n=== ¿De qué fuente vienen? source breakdown de products benchmark ===")
for pid in (7431, 5945, 5929, 7425):
    r = c.execute("SELECT id, title, marketplace, marketplace_id, url_canonical, affiliate_link FROM products WHERE id=?", (pid,)).fetchone()
    if r:
        print(f"  product {pid}: mkt={r['marketplace']} mlid/asin={r['marketplace_id']} aff={bool(r['affiliate_link'])}")
        print(f"     url={r['url_canonical']}")
        print(f"     title={r['title'][:60]}")
