#!/usr/bin/env python3
"""Sanitizer/auditor READ-ONLY de ML sospechoso + replay del gate nuevo.

- Aplica el gate ML nuevo (dry-run) a pending ML y reporta cuáles bloquearía.
- Reporta sent ML recientes sospechosos (descuento>=70, prev inventado, etc.).
NO modifica nada (ni pending ni sent ni published).
"""
import json, sqlite3, sys
sys.path.insert(0, "src")
from ofertas_hunter.models import OutboxItem, OutboxState, OutboxType
from ofertas_hunter.publishing.whatsapp_publisher import WhatsAppPublisher

DB = "data/ofertas_hunter.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=30000")


class _R:
    success=True; dry_run=True; raw={}
class _C:
    dry_run=True
    async def send_media(self,*a,**k): return _R()

pub = WhatsAppPublisher(_C(), "g@g.us", enabled=True,
                        mercadolibre_affiliate_required=True,
                        ml_extreme_discount_threshold=70.0)

def item_from(row):
    try:
        p = json.loads(row["message_payload_json"])
    except Exception:
        p = {}
    return OutboxItem(id=row["id"], offer_id=row["offer_id"] if "offer_id" in row.keys() else 0,
                      type=row["type"] if "type" in row.keys() else "normal",
                      message_payload=p, state="pending"), p

print("===== PENDING ML: ¿qué bloquearía el gate nuevo? =====")
rows = c.execute("SELECT id, offer_id, type, message_payload_json FROM outbox WHERE state='pending'").fetchall()
ml_pending = 0; blocked = 0; by_reason = {}
for r in rows:
    it, p = item_from(r)
    if (p.get("marketplace") or "").lower() != "mercadolibre":
        continue
    ml_pending += 1
    out = pub._ml_price_gate(it)
    if out is not None:
        blocked += 1
        by_reason[out.discard_reason] = by_reason.get(out.discard_reason, 0) + 1
        print(f"  BLOCK id={r['id']} reason={out.discard_reason} "
              f"cur={p.get('current_price')} prev={p.get('previous_price')} disc={p.get('discount_percent')} "
              f"| {(p.get('title') or '')[:45]}")
print(f"\nML pending total={ml_pending} | bloqueados por gate nuevo={blocked} | por razon={by_reason}")

print("\n===== SENT ML recientes SOSPECHOSOS (read-only, NO se tocan) =====")
rows = c.execute(
    "SELECT pm.id pm_id, pm.outbox_id, pm.sent_at, o.message_payload_json, pm.message_text "
    "FROM published_messages pm JOIN outbox o ON pm.outbox_id=o.id "
    "WHERE pm.success=1 AND o.message_payload_json LIKE '%mercadolibre%' "
    "ORDER BY pm.id DESC LIMIT 60"
).fetchall()
susp = 0
for r in rows:
    try:
        p = json.loads(r["message_payload_json"])
    except Exception:
        p = {}
    cur = p.get("current_price"); prev = p.get("previous_price"); disc = p.get("discount_percent")
    # sospechoso: previous None pero discount alto (precio inventado), o ratio>3, o disc>=70
    suspicious = False; why = []
    if prev is None and isinstance(disc,(int,float)) and disc >= 50:
        suspicious = True; why.append("previous_price=None+discount")
    if isinstance(disc,(int,float)) and disc >= 70:
        suspicious = True; why.append("discount>=70")
    if isinstance(cur,(int,float)) and isinstance(prev,(int,float)) and cur>0 and prev/cur > 3:
        suspicious = True; why.append("ratio>3")
    if suspicious:
        susp += 1
        # extraer "Antes" del texto real
        import re as _re
        m = _re.search(r"Antes:\s*~?\$([\d,\.]+)", r["message_text"] or "")
        antes = m.group(1) if m else "-"
        print(f"  outbox={r['outbox_id']} sent={r['sent_at'][:19]} cur={cur} prev_payload={prev} "
              f"antes_msg=${antes} disc={disc} why={why}")
        print(f"     title: {(p.get('title') or '')[:55]}  link={p.get('affiliate_url')}")
print(f"\nSENT ML sospechosos (recientes): {susp}")
