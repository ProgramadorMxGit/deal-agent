#!/usr/bin/env python3
"""FASE 1 READ-ONLY: cómo ML muestra precios anteriores reales y qué hay en DB."""
import json, sqlite3
DB = "data/ofertas_hunter.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=30000")

print("=== dom_snapshots por marketplace ===")
for r in c.execute("SELECT marketplace, COUNT(*) n FROM dom_snapshots GROUP BY marketplace"):
    print(f"  {r['marketplace']}: {r['n']}")

print("\n=== price_observations ML con previous_price NOT NULL (precio anterior real capturado) ===")
rows = c.execute(
    """
    SELECT po.id, po.current_price, po.previous_price, po.discount_percent, po.observed_at,
           p.title, p.url_canonical, p.marketplace
    FROM price_observations po JOIN products p ON p.id=po.product_id
    WHERE p.marketplace='mercadolibre' AND po.previous_price IS NOT NULL
    ORDER BY po.id DESC LIMIT 15
    """
).fetchall()
print(f"  encontrados: {len(rows)}")
for r in rows:
    disc_calc = round((r['previous_price']-r['current_price'])/r['previous_price']*100) if (r['previous_price'] and r['current_price'] and r['previous_price']>0) else None
    print(f"  cur={r['current_price']} prev={r['previous_price']} disc={r['discount_percent']} calc={disc_calc}% | {(r['title'] or '')[:45]}")

print("\n=== outbox ML pending: ¿cuántos tienen previous_price real (no None)? ===")
rows = c.execute("SELECT message_payload_json FROM outbox WHERE state='pending'").fetchall()
ml=0; prev_real=0; prev_none=0
for r in rows:
    try: p=json.loads(r["message_payload_json"])
    except Exception: continue
    if (p.get("marketplace") or "").lower()!="mercadolibre": continue
    ml+=1
    if p.get("previous_price") is not None: prev_real+=1
    else: prev_none+=1
print(f"  ML pending={ml} | con previous_price real={prev_real} | con None={prev_none}")

print("\n=== muestra ML pending con previous_price real (candidatos legítimos) ===")
shown=0
for r in rows:
    try: p=json.loads(r["message_payload_json"])
    except Exception: continue
    if (p.get("marketplace") or "").lower()!="mercadolibre": continue
    if p.get("previous_price") is None: continue
    cur=p.get("current_price"); prev=p.get("previous_price"); disc=p.get("discount_percent")
    calc=round((prev-cur)/prev*100) if (prev and cur and prev>0) else None
    print(f"  cur={cur} prev={prev} disc={disc} calc={calc}% verif={p.get('ml_previous_price_verified')} | {(p.get('title') or '')[:42]}")
    shown+=1
    if shown>=10: break
if shown==0:
    print("  (ninguno con previous_price real en pending)")
