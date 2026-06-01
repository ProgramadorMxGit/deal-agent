#!/usr/bin/env python3
"""FASE 5: encontrar ML legítimos que PASAN el gate nuevo (READ-ONLY)."""
import json, sqlite3, sys
sys.path.insert(0, "src")
from ofertas_hunter.models import OutboxItem
from ofertas_hunter.publishing.whatsapp_publisher import WhatsAppPublisher

DB = "data/ofertas_hunter.db"
class _R:
    success=True; dry_run=True; raw={}
class _C:
    dry_run=True
    async def send_media(self,*a,**k): return _R()
pub = WhatsAppPublisher(_C(), "g@g.us", enabled=True,
                        mercadolibre_affiliate_required=True,
                        ml_extreme_discount_threshold=70.0)

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=30000")

# pending + sent recientes ML, ver cuáles pasan el gate
rows = c.execute(
    "SELECT id, offer_id, type, state, message_payload_json FROM outbox "
    "WHERE state IN ('pending','sent','deferred') AND message_payload_json LIKE '%mercadolibre%' "
    "ORDER BY id DESC LIMIT 200"
).fetchall()
passed = []
for r in rows:
    try: p = json.loads(r["message_payload_json"])
    except Exception: continue
    if (p.get("marketplace") or "").lower() != "mercadolibre": continue
    if r["type"] != "normal": continue
    it = OutboxItem(id=r["id"], offer_id=r["offer_id"], type="normal", message_payload=p, state=r["state"])
    out = pub._ml_price_gate(it)
    if out is None:
        passed.append((r["id"], r["state"], p))

print(f"=== ML que PASAN el gate nuevo: {len(passed)} ===")
print(f"{'id':>5} {'state':>8} {'cur':>8} {'prev':>9} {'disc':>5} {'prevVerif':>9} {'curVerif':>8}  titulo")
for cid, st, p in passed[:20]:
    print(f"{cid:>5} {st:>8} {str(p.get('current_price')):>8} {str(p.get('previous_price')):>9} "
          f"{str(p.get('discount_percent')):>5} {str(p.get('ml_previous_price_verified')):>9} "
          f"{str(p.get('current_price_verified')):>8}  {(p.get('title') or '')[:38]}")
if not passed:
    print("  (ninguno pasa todavía — los nuevos ML con strikethrough verificado pasarán al re-crawlear)")
