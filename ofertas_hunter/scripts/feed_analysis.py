#!/usr/bin/env python3
"""Analiza la ALIMENTACIÓN del outbox (READ-ONLY) — Tarea 8.

Responde por qué el pending está mono-categoría aunque offers.eligible es variado.
"""
import json
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, "src")
from ofertas_hunter.dispatching.diversity_metadata import infer_offer_category

DB = "data/ofertas_hunter.db"
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-05-30T21:00:00Z"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=30000")

print(f"===== discarded_candidates por razon+source (desde {SINCE}) =====")
for r in c.execute(
    "SELECT reason, source, COUNT(*) n FROM discarded_candidates WHERE created_at>=? "
    "GROUP BY reason, source ORDER BY n DESC LIMIT 25", (SINCE,)
):
    print(f"  {r['n']:>5}  {r['source']:<22} {r['reason']}")

print(f"\n===== outbox ENCOLADO recientemente (enqueued_at>={SINCE}) por categoria/source =====")
rows = c.execute(
    "SELECT o.message_payload_json, o.state FROM outbox o WHERE o.enqueued_at>=?", (SINCE,)
).fetchall()
cat = Counter(); src = Counter(); mkt = Counter(); state = Counter()
for r in rows:
    try: p = json.loads(r["message_payload_json"])
    except Exception: p = {}
    cat[infer_offer_category(p.get("title") or "", p, p.get("marketplace"), p.get("source")).normalized] += 1
    src[p.get("source") or "?"] += 1
    mkt[p.get("marketplace") or "?"] += 1
    state[r["state"]] += 1
print(f"  total encolado en ventana: {len(rows)}")
print("  por categoria:", dict(cat.most_common(12)))
print("  por source:", dict(src.most_common()))
print("  por marketplace:", dict(mkt.most_common()))
print("  por estado actual:", dict(state.most_common()))

print(f"\n===== offers.eligible: categoria vs si tiene outbox pending =====")
# cuantas offers eligible NO tienen un outbox asociado en pending (variedad atrapada)
rows = c.execute(
    "SELECT p.title, p.marketplace, "
    "  (SELECT COUNT(*) FROM outbox o WHERE o.offer_id=f.id) AS in_outbox "
    "FROM offers f JOIN products p ON p.id=f.product_id "
    "WHERE f.state='eligible' LIMIT 1000"
).fetchall()
cat_all = Counter(); cat_noout = Counter()
for r in rows:
    cn = infer_offer_category(r["title"] or "", {}).normalized
    cat_all[cn] += 1
    if not r["in_outbox"]:
        cat_noout[cn] += 1
print(f"  (muestra {len(rows)} offers eligible)")
print("  TODAS por categoria:", dict(cat_all.most_common(12)))
print("  SIN outbox (variedad atrapada) por categoria:", dict(cat_noout.most_common(12)))

print(f"\n===== pending por source (de donde viene la saturacion) =====")
rows = c.execute("SELECT message_payload_json FROM outbox WHERE state='pending'").fetchall()
psrc = Counter(); pcat_src = Counter()
for r in rows:
    try: p = json.loads(r["message_payload_json"])
    except Exception: p = {}
    s = p.get("source") or "?"
    cn = infer_offer_category(p.get("title") or "", p, p.get("marketplace"), s).normalized
    psrc[s] += 1
    pcat_src[f"{s}:{cn}"] += 1
print("  pending por source:", dict(psrc.most_common()))
print("  pending por source:categoria:", dict(pcat_src.most_common(12)))
