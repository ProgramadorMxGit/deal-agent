"""Siembra el frontier Amazon desde config/seeds/amazon.json sin browser.

Replica la lógica de DiscoveryAgent.seed_from_config sin necesitar Playwright,
lo cual es ideal para auto-bootstrap inicial.
"""
import json
import sys
sys.path.insert(0, 'src')

from ofertas_hunter.db import connect
from ofertas_hunter.exploration.frontier import FrontierRepo
from ofertas_hunter.exploration.url_classifier import classify

conn = connect()
frontier = FrontierRepo(conn)

with open("config/seeds/amazon.json", encoding="utf-8") as f:
    urls = json.load(f)

print(f"Sembrando Amazon desde {len(urls)} URLs en config/seeds/amazon.json...")
seeded = 0
skipped_unknown = 0
skipped_other_marketplace = 0
already_existed = 0

for url in urls:
    info = classify(url)
    if info.marketplace != "amazon":
        skipped_other_marketplace += 1
        continue
    if info.kind == "unknown":
        skipped_unknown += 1
        continue
    if frontier.add_classified(info):
        seeded += 1
    else:
        already_existed += 1

print(f"  Sembradas             : {seeded}")
print(f"  Ya existian           : {already_existed}")
print(f"  Skipped (unknown kind): {skipped_unknown}")
print(f"  Skipped (otro mkt)    : {skipped_other_marketplace}")

conn.commit()

# Verificación final
print()
print("=== Frontier Amazon: estado final ===")
n = frontier.count_pending("amazon")
print(f"  count_pending(amazon) = {n}")

cur = conn.execute(
    "SELECT url_type, COUNT(*) as n FROM frontier WHERE marketplace='amazon' "
    "GROUP BY url_type ORDER BY n DESC"
)
for r in cur:
    print(f"    {r[0]}: {r[1]}")
