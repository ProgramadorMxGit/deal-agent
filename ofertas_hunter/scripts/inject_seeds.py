#!/usr/bin/env python3
"""Inyecta las nuevas seeds category-aware al frontier con score ALTO.

Necesario porque auto_seed solo siembra cuando el frontier está vacío, y el
frontier ML tiene 24k URLs. Esto agrega SOLO las seeds nuevas (no toca las
existentes), con score alto para que la discovery las procese pronto.

Modo: 'apply' para escribir; sin args = dry-run (cuenta).
"""
import json, sys
sys.path.insert(0, "src")
from ofertas_hunter.db import connect
from ofertas_hunter.exploration.frontier import FrontierRepo
from ofertas_hunter.exploration.url_classifier import classify

APPLY = len(sys.argv) > 1 and sys.argv[1] == "apply"

import json as _json
from pathlib import Path
root = Path(".")
ml = _json.loads((root / "config/seeds/mercadolibre.json").read_text(encoding="utf-8"))
amz = _json.loads((root / "config/seeds/amazon.json").read_text(encoding="utf-8"))

# Solo las seeds "nuevas" (deals/ofertas/Descuento/pct-off) — alto valor
def is_new_seed(u):
    return any(k in u for k in ("ofertas/cocina", "ofertas/organizacion", "ofertas/electrodomesticos",
        "ofertas/herramientas", "ofertas/deportes", "ofertas/bebes", "ofertas/computacion",
        "ofertas/electronica", "ofertas/muebles", "ofertas/jardin", "Descuento_50-100",
        "deals-collection", "pct-off-with-tax"))

new_urls = [u for u in (ml + amz) if is_new_seed(u)]
print(f"seeds nuevas candidatas: {len(new_urls)}")

if not APPLY:
    # dry-run: clasificar
    for u in new_urls[:6]:
        ci = classify(u)
        print(f"  [{ci.marketplace}/{ci.kind}] {u[:70]}")
    print("(DRY-RUN; usa 'apply' para inyectar al frontier con score alto)")
    sys.exit(0)

conn = connect()
fr = FrontierRepo(conn)
added = 0
for u in new_urls:
    ci = classify(u)
    if ci.kind in ("unknown", ""):
        continue
    # score alto (20) para que se procesen antes que el backlog
    rid = fr.add(u, kind=ci.kind, score=20.0)
    if rid:
        added += 1
conn.commit()
print(f"INYECTADAS al frontier: {added} (score=20.0)")
# conteo
import sqlite3
print("frontier ahora:", conn.execute("SELECT marketplace, COUNT(*) FROM frontier GROUP BY marketplace").fetchall())
conn.close()
