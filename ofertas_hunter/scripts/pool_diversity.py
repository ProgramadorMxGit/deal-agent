#!/usr/bin/env python3
"""Analiza la VARIEDAD del pool elegible actual (READ-ONLY).

Responde: ¿el override se dispara porque el selector falla, o porque el
pool realmente está mono-categoría? Mira outbox pending + in_flight y
clasifica por categoría/marca normalizada.
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
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=20)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=20000")

for state in ("pending", "in_flight"):
    rows = c.execute(
        "SELECT id, message_payload_json FROM outbox WHERE state=?", (state,)
    ).fetchall()
    cats = Counter(); brands = Counter(); mkts = Counter(); fams = Counter()
    for r in rows:
        try:
            p = json.loads(r["message_payload_json"])
        except Exception:
            p = {}
        title = p.get("title") or ""
        cats[infer_offer_category(title, p, p.get("marketplace"), p.get("source")).normalized] += 1
        brands[normalize_brand(p.get("brand"), title) or "-"] += 1
        mkts[p.get("marketplace") or "?"] += 1
        fams[product_family(title, p.get("brand"))] += 1
    print(f"===== outbox '{state}' (n={len(rows)}) =====")
    print("  categorías:", dict(cats.most_common()))
    print("  marketplaces:", dict(mkts.most_common()))
    print("  top marcas:", dict(brands.most_common(8)))
    print("  familias distintas:", len(fams), "| top:", dict(fams.most_common(5)))
    print()

# también: cuántas offers eligible hay por categoría (más amplio que outbox)
print("===== offers state=eligible: muestra de categorías (vía products) =====")
rows = c.execute(
    "SELECT p.title FROM offers f JOIN products p ON p.id=f.product_id "
    "WHERE f.state='eligible' LIMIT 500"
).fetchall()
cats = Counter()
for r in rows:
    cats[infer_offer_category(r["title"] or "", {}).normalized] += 1
print(f"  (muestra {len(rows)} offers eligible)")
print("  categorías:", dict(cats.most_common()))
