"""Inspecciona el historial de precios de un ASIN específico."""
import json, sqlite3, sys
from pathlib import Path

DB = Path("/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db")
if not DB.exists():
    DB = Path("data/ofertas_hunter.db")

ASIN = sys.argv[1] if len(sys.argv) > 1 else "B0FBKNHMN7"
c = sqlite3.connect(str(DB)); c.row_factory = sqlite3.Row

print(f"=== ASIN: {ASIN} ===\n")

# Producto
prod = c.execute("SELECT * FROM products WHERE marketplace_id=?", (ASIN,)).fetchone()
if prod:
    print(f"product#{prod['id']} title={prod['title'][:60]}")
    print(f"  first_seen={prod['first_seen_at']}  last_seen={prod['last_seen_at']}")
    print()

# Historial de precios
obs = c.execute(
    "SELECT current_price, previous_price, discount_percent, observed_at "
    "FROM price_observations WHERE product_id=? ORDER BY id",
    (prod['id'] if prod else -1,)
).fetchall()
print(f"Historial de precios ({len(obs)} observaciones):")
for r in obs:
    print(f"  {r['observed_at']}  cur={r['current_price']}  prev={r['previous_price']}  disc={r['discount_percent']}%")
print()

# Outbox items
ob = c.execute(
    "SELECT id, type, state, enqueued_at, attempts, "
    "json_extract(message_payload_json,'$.current_price') cur, "
    "json_extract(message_payload_json,'$.previous_price') prev, "
    "json_extract(message_payload_json,'$.discount_percent') disc "
    "FROM outbox WHERE json_extract(message_payload_json,'$.item_id')=? "
    "OR json_extract(message_payload_json,'$.asin')=? ORDER BY id",
    (ASIN, ASIN)
).fetchall()
print(f"Outbox items ({len(ob)}):")
for r in ob:
    print(f"  ob#{r['id']} [{r['type']}] {r['state']} enqueued={r['enqueued_at']} cur={r['cur']} prev={r['prev']} disc={r['disc']}%")
print()

# Published
pub = c.execute(
    "SELECT pm.id, pm.sent_at, pm.success FROM published_messages pm "
    "JOIN outbox o ON pm.outbox_id=o.id "
    "WHERE json_extract(o.message_payload_json,'$.item_id')=? "
    "OR json_extract(o.message_payload_json,'$.asin')=? ORDER BY pm.id",
    (ASIN, ASIN)
).fetchall()
print(f"Publicaciones ({len(pub)}):")
for r in pub:
    print(f"  pm#{r['id']} ok={r['success']} {r['sent_at']}")
