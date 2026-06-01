#!/usr/bin/env python3
"""FASE 6: saneamiento autorizado de ML pending sospechosos (sin verificación).

Modo por defecto: DRY-RUN (solo imprime candidatos y conteos).
Con argumento 'apply': mueve a state='discarded' con reject reason, escribiendo
discarded_reason en el payload. SOLO toca ML pending. NUNCA toca sent/Amazon/
deferred verificados/published_messages.
"""
import json, sqlite3, sys
sys.path.insert(0, "src")
from ofertas_hunter.models import OutboxItem, OutboxState, OutboxType
from ofertas_hunter.publishing.whatsapp_publisher import WhatsAppPublisher

APPLY = len(sys.argv) > 1 and sys.argv[1] == "apply"
DB = "data/ofertas_hunter.db"

class _R:
    success=True; dry_run=True; raw={}
class _C:
    dry_run=True
    async def send_media(self,*a,**k): return _R()
pub = WhatsAppPublisher(_C(), "g@g.us", enabled=True,
                        mercadolibre_affiliate_required=True,
                        ml_extreme_discount_threshold=70.0)

mode = "rw" if APPLY else "ro"
if APPLY:
    c = sqlite3.connect(DB, timeout=30)
else:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=30000")

rows = c.execute(
    "SELECT id, offer_id, type, state, message_payload_json FROM outbox WHERE state='pending'"
).fetchall()

candidates = []
for r in rows:
    try: p = json.loads(r["message_payload_json"])
    except Exception: continue
    if (p.get("marketplace") or "").lower() != "mercadolibre":
        continue
    if r["type"] != "normal":
        continue
    it = OutboxItem(id=r["id"], offer_id=r["offer_id"], type=r["type"],
                    message_payload=p, state="pending")
    out = pub._ml_price_gate(it)
    if out is not None:
        candidates.append((r["id"], out.discard_reason, p))

print(f"=== FASE 6 — saneamiento ML pending ({'APPLY' if APPLY else 'DRY-RUN'}) ===")
print(f"ML pending normal total evaluados: {sum(1 for r in rows)} (todos los pending)")
print(f"candidatos a sanear (bloqueados por gate): {len(candidates)}")
by_reason = {}
for cid, reason, p in candidates:
    by_reason[reason] = by_reason.get(reason, 0) + 1
    print(f"  id={cid} reason={reason} cur={p.get('current_price')} prev={p.get('previous_price')} "
          f"disc={p.get('discount_percent')} verif={p.get('ml_previous_price_verified')} | {(p.get('title') or '')[:42]}")
print(f"\npor reason: {by_reason}")

# Sanity: confirmar que ninguno es sent/amazon
for cid, reason, p in candidates:
    assert (p.get("marketplace") or "").lower() == "mercadolibre", "NO-ML detectado!"

if APPLY and candidates:
    for cid, reason, p in candidates:
        p2 = dict(p)
        p2["discarded_reason"] = reason
        p2["sanitized_by"] = "ml_pending_sanitizer"
        c.execute(
            "UPDATE outbox SET state='discarded', message_payload_json=? "
            "WHERE id=? AND state='pending' AND type='normal'",
            (json.dumps(p2, ensure_ascii=False), cid),
        )
    c.commit()
    print(f"\nAPLICADO: {len(candidates)} ML pending -> discarded")
elif not APPLY:
    print("\n(DRY-RUN: no se modificó nada. Ejecuta con 'apply' para sanear.)")

# conteo final
final = c.execute("SELECT state, COUNT(*) n FROM outbox WHERE state IN ('pending','discarded') GROUP BY state").fetchall()
print("conteo outbox (pending/discarded):", {r["state"]: r["n"] for r in final})
