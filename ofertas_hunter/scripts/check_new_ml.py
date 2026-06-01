#!/usr/bin/env python3
"""Verifica que items ML encolados DESPUÉS del deploy llevan los flags nuevos."""
import json, sqlite3
DB = "data/ofertas_hunter.db"
SINCE = "2026-05-31T03:51:00Z"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=30000")
rows = c.execute(
    "SELECT id, state, enqueued_at, message_payload_json FROM outbox "
    "WHERE enqueued_at >= ? ORDER BY id DESC LIMIT 200", (SINCE,)
).fetchall()
ml = 0; with_flags = 0; verified_true = 0; samples = []
for r in rows:
    try: p = json.loads(r["message_payload_json"])
    except Exception: continue
    if (p.get("marketplace") or "").lower() != "mercadolibre":
        continue
    ml += 1
    if "ml_previous_price_verified" in p:
        with_flags += 1
        if p.get("ml_previous_price_verified"):
            verified_true += 1
        if len(samples) < 8:
            samples.append((r["id"], r["state"], p.get("current_price"), p.get("previous_price"),
                            p.get("discount_percent"), p.get("ml_previous_price_verified"),
                            (p.get("title") or "")[:38]))
print(f"ML encolados desde deploy: {ml}")
print(f"  con flags nuevos: {with_flags}")
print(f"  con ml_previous_price_verified=True: {verified_true}")
print("\nMuestra (id, state, cur, prev, disc, prev_verified, title):")
for s in samples:
    print("  ", s)
