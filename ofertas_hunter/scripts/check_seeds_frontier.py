#!/usr/bin/env python3
"""¿Las nuevas seeds llegaron al frontier? (READ-ONLY)"""
import sqlite3
DB = "data/ofertas_hunter.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=30000")

patterns = ["ofertas/cocina", "ofertas/organizacion", "ofertas/herramientas",
            "Descuento_50-100", "deals-collection", "pct-off-with-tax",
            "fuente+de+poder", "termo+acero", "cesto+ropa"]
print("=== nuevas seeds en frontier ===")
for pat in patterns:
    r = c.execute("SELECT COUNT(*) n FROM frontier WHERE url_canonical LIKE ?", (f"%{pat}%",)).fetchone()
    print(f"  {pat}: {r['n']}")

print("\n=== frontier totales por marketplace/tipo ===")
for r in c.execute("SELECT marketplace, url_type, COUNT(*) n FROM frontier GROUP BY marketplace, url_type ORDER BY n DESC"):
    print(f"  {r['marketplace']}/{r['url_type']}: {r['n']}")

print("\n=== visited recientes con nuevas seeds ===")
for pat in ["ofertas/cocina", "Descuento_50-100", "deals-collection", "pct-off-with-tax"]:
    r = c.execute("SELECT COUNT(*) n FROM visited_urls WHERE url_canonical LIKE ?", (f"%{pat}%",)).fetchone()
    print(f"  visited {pat}: {r['n']}")

print("\n=== runtime_events ml_discovery_prefilter recientes ===")
for r in c.execute("SELECT created_at, payload_json FROM runtime_events WHERE kind='ml_discovery_prefilter' ORDER BY id DESC LIMIT 3"):
    print(f"  {r['created_at']} {r['payload_json'][:160]}")
