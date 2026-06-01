import sqlite3, json
DB = "data/ofertas_hunter.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=30000")

print("=== ml_discovery_prefilter (ult 5) ===")
n = 0
for r in c.execute("SELECT created_at, payload_json FROM runtime_events WHERE kind='ml_discovery_prefilter' ORDER BY id DESC LIMIT 5"):
    print(" ", r["created_at"], r["payload_json"][:200]); n += 1
if n == 0:
    print("  (ninguno)")

print("\n=== frontier_category_bias_applied (ult 3) ===")
for r in c.execute("SELECT created_at, payload_json FROM runtime_events WHERE kind='frontier_category_bias_applied' ORDER BY id DESC LIMIT 3"):
    print(" ", r["created_at"], r["payload_json"][:180])

print("\n=== deals/ofertas en frontier (siguen ahí = no procesadas) ===")
for r in c.execute("SELECT url_type, score, COUNT(*) n FROM frontier WHERE url_canonical LIKE '%ofertas/%' OR url_canonical LIKE '%Descuento_50-100%' OR url_canonical LIKE '%deals-collection%' OR url_canonical LIKE '%pct-off%' GROUP BY url_type, score"):
    print(f"  type={r['url_type']} score={r['score']} count={r['n']}")

print("\n=== visited con score alto / nuevas ===")
for pat in ["ofertas/", "Descuento_50-100", "deals-collection", "pct-off"]:
    r = c.execute("SELECT COUNT(*) n FROM visited_urls WHERE url_canonical LIKE ?", (f"%{pat}%",)).fetchone()
    print(f"  visited {pat}: {r['n']}")

print("\n=== muestra products nuevos (ult 10 por id) ===")
for r in c.execute("SELECT id, title, marketplace FROM products ORDER BY id DESC LIMIT 10"):
    print(f"  {r['id']} [{r['marketplace']}] {(r['title'] or '')[:50]}")
