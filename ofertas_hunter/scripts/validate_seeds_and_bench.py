#!/usr/bin/env python3
"""Valida que las nuevas seeds clasifican OK y simula los 4 benchmarks contra gates."""
import sys
sys.path.insert(0, "src")
from ofertas_hunter.exploration.url_classifier import classify
from ofertas_hunter.models import OutboxItem, OutboxType
from ofertas_hunter.publishing.whatsapp_publisher import WhatsAppPublisher

print("=== Clasificación de nuevas seeds (no debe haber 'unknown') ===")
samples = [
    "https://www.mercadolibre.com.mx/ofertas/cocina",
    "https://listado.mercadolibre.com.mx/cesto-ropa-sucia_Descuento_50-100",
    "https://www.amazon.com.mx/deals?bubble-id=deals-collection-home-kitchen",
    "https://www.amazon.com.mx/s?k=fuente+de+poder+pc&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=termo+acero+inoxidable&rh=p_n_pct-off-with-tax%3A50-",
]
for u in samples:
    ci = classify(u)
    print(f"  [{ci.marketplace}/{ci.kind} score={ci.score}] {u[:65]}")

print("\n=== Benchmark contra gates (simulación de PDP REAL con datos del canal) ===")
class _R: success=True; dry_run=True; raw={}
class _C:
    dry_run=True
    async def send_media(self,*a,**k): return _R()
pub = WhatsAppPublisher(_C(), "g@g.us", enabled=True,
                        mercadolibre_affiliate_required=True, amazon_affiliate_required=True,
                        ml_extreme_discount_threshold=70.0)

# Simulamos lo que el bot capturaría SI crawlea el PDP y extrae old_price real verificado.
benches = [
    {"label": "Cesto bambú AG Box (ML)", "mkt": "mercadolibre", "cur": 406, "prev": 999, "disc": 59,
     "aff": "https://meli.la/2PD6zoH", "ml_prev_verif": True, "cur_verif": True},
    {"label": "Olla Lamex (AMZ)", "mkt": "amazon", "cur": 624, "prev": 1249, "disc": 50,
     "aff": "https://amzn.to/431O2fk", "old_verif": True},
    {"label": "Corsair RM750x (AMZ)", "mkt": "amazon", "cur": 1539.76, "prev": 3364, "disc": 54,
     "aff": "https://amzn.to/xyz", "old_verif": True},
    {"label": "Stanley Quencher (AMZ)", "mkt": "amazon", "cur": 469, "prev": 1099, "disc": 57,
     "aff": "https://amzn.to/abc", "old_verif": True},
]
for b in benches:
    payload = {
        "title": b["label"], "marketplace": b["mkt"],
        "current_price": b["cur"], "previous_price": b["prev"], "discount_percent": b["disc"],
        "affiliate_url": b["aff"], "url": b["aff"], "image_url": "https://x/i.jpg",
    }
    if b["mkt"] == "amazon":
        payload["old_price_verified"] = b.get("old_verif", False)
    else:
        payload["ml_previous_price_verified"] = b.get("ml_prev_verif", False)
        payload["current_price_verified"] = b.get("cur_verif", False)
        payload["discount_percent_verified"] = True
    item = OutboxItem(id=1, offer_id=1, type=OutboxType.NORMAL.value, message_payload=payload, state="pending")
    amz = pub._amazon_gate(item)
    ml = pub._ml_price_gate(item)
    blocked = amz or ml
    verdict = f"BLOQUEADO ({(blocked.discard_reason if blocked else '')})" if blocked else "PASA (publicable)"
    print(f"  {b['label']}: disc={b['disc']}% -> {verdict}")
