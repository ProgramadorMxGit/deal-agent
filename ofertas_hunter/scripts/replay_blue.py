#!/usr/bin/env python3
"""Replay del caso real Blu-e contra el gate ML nuevo (Tarea 9)."""
import sys
sys.path.insert(0, "src")
from ofertas_hunter.models import OutboxItem, OutboxState, OutboxType
from ofertas_hunter.publishing.whatsapp_publisher import WhatsAppPublisher


class _R:
    success = True; dry_run = True; raw = {}
class _C:
    dry_run = True
    async def send_media(self, *a, **k): return _R()


pub = WhatsAppPublisher(_C(), "g@g.us", enabled=True,
                        mercadolibre_affiliate_required=True,
                        ml_extreme_discount_threshold=70.0)

# Caso EXACTO publicado: Blu-e 2.2kg Chocolate, current=650, discount=78, previous None
caso = {
    "title": "Proteina Whey Hidrolizada Isolate Blu-e 2.2kg Sabores Chocolate",
    "marketplace": "mercadolibre",
    "current_price": 650.0,
    "previous_price": None,
    "discount_percent": 78.0,
    "affiliate_url": "https://meli.la/32TDYSu",
    "image_url": "https://x/img.jpg",
    "url": "https://meli.la/32TDYSu",
    "item_id": "MLM67461444",
}
item = OutboxItem(id=999, offer_id=999, type=OutboxType.NORMAL.value,
                  message_payload=caso, state=OutboxState.PENDING.value)
out = pub._ml_price_gate(item)
print("=== CASO REAL Blu-e (current=650, discount=78, previous=None) ===")
print("  resultado gate:", "BLOQUEADO" if out else "PASA")
if out:
    print("  reject_reason:", out.discard_reason)
assert out is not None and out.discard_reason == "ml_no_verified_previous_price", "DEBE bloquear"
print("  -> CONFIRMADO: no se republicaria como 78% (sin precio anterior verificado)")

# Variante: si tuviera previous verificado real y descuento moderado, publica
caso_ok = dict(caso, previous_price=1300.0, discount_percent=50.0,
               ml_previous_price_verified=True, current_price_verified=True)
item2 = OutboxItem(id=998, offer_id=998, type=OutboxType.NORMAL.value,
                   message_payload=caso_ok, state=OutboxState.PENDING.value)
out2 = pub._ml_price_gate(item2)
print("\n=== Variante con previous verificado real (1300->650, 50%) ===")
print("  resultado gate:", "BLOQUEADO" if out2 else "PASA (publicable)")
