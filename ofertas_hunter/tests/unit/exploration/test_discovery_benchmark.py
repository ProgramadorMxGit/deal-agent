"""Tests de discovery autónomo + benchmark (TAREA 11)."""
from __future__ import annotations

import json
from pathlib import Path

from ofertas_hunter.exploration.url_classifier import classify
from ofertas_hunter.exploration.frontier_category import category_of_url, adjusted_score
from ofertas_hunter.models import OutboxItem, OutboxType
from ofertas_hunter.publishing.whatsapp_publisher import WhatsAppPublisher


class _R:
    success = True; dry_run = True; raw = {}
class _C:
    dry_run = True
    async def send_media(self, *a, **k): return _R()


def _pub():
    return WhatsAppPublisher(_C(), "g@g.us", enabled=True,
                             mercadolibre_affiliate_required=True,
                             amazon_affiliate_required=True,
                             ml_extreme_discount_threshold=70.0)


def _item(payload):
    return OutboxItem(id=1, offer_id=1, type=OutboxType.NORMAL.value,
                      message_payload=payload, state="pending")


# E — frontier boost prioriza URL de tecnología/hogar deficitaria
def test_E_frontier_boost_prioritizes_deficit():
    s_tech = adjusted_score(5.0, "tecnologia", ["tecnologia", "hogar"], ["belleza"],
                            deficit_boost=3.0, saturated_penalty=0.35)
    s_belleza = adjusted_score(5.0, "belleza", ["tecnologia", "hogar"], ["belleza"],
                               deficit_boost=3.0, saturated_penalty=0.35)
    assert s_tech > s_belleza
    assert s_tech == 15.0
    assert round(s_belleza, 2) == 1.75


# F — frontier penalty baja belleza/proteína saturada
def test_F_frontier_penalty_lowers_saturated():
    base = 10.0
    s = adjusted_score(base, "proteina/suplementos", ["hogar"], ["proteina/suplementos"],
                       deficit_boost=3.0, saturated_penalty=0.35)
    assert s < base
    assert round(s, 2) == 3.5


# G — nuevas seeds clasifican OK (no unknown)
def test_G_new_seeds_classify():
    cases = [
        ("https://www.mercadolibre.com.mx/ofertas/cocina", "mercadolibre", "deals"),
        ("https://listado.mercadolibre.com.mx/cesto-ropa-sucia_Descuento_50-100", "mercadolibre", "listing"),
        ("https://www.amazon.com.mx/deals?bubble-id=deals-collection-home-kitchen", "amazon", "deals"),
        ("https://www.amazon.com.mx/s?k=fuente+de+poder+pc&rh=p_n_pct-off-with-tax%3A50-", "amazon", "listing"),
    ]
    for url, mkt, kind in cases:
        ci = classify(url)
        assert ci.marketplace == mkt, url
        assert ci.kind == kind, f"{url} -> {ci.kind}"


def test_G_url_category_mapping():
    # "cesto-ropa-sucia" puede mapear a hogar/otro/ropa (heurística de frontier,
    # solo afecta priorización, no gates).
    assert category_of_url("https://listado.mercadolibre.com.mx/cesto-ropa-sucia_Descuento_50-100") in ("hogar", "otro", "ropa")
    assert category_of_url("https://www.amazon.com.mx/s?k=fuente+de+poder+pc") == "tecnologia"
    assert category_of_url("https://listado.mercadolibre.com.mx/olla-cocina_Descuento_50-100") == "hogar"


# H — Amazon benchmark con old_price verificado pasa
def test_H_amazon_benchmark_verified_passes():
    pub = _pub()
    payload = {
        "title": "Olla Cinco by Lamex 24cm", "marketplace": "amazon",
        "current_price": 624.0, "previous_price": 1249.0, "discount_percent": 50,
        "affiliate_url": "https://amzn.to/x", "url": "https://amzn.to/x",
        "image_url": "https://x/i.jpg", "old_price_verified": True,
    }
    assert pub._amazon_gate(_item(payload)) is None  # pasa


# I — ML benchmark SIN previous verificado bloquea (no inventar)
def test_I_ml_benchmark_unverified_blocks():
    pub = _pub()
    payload = {
        "title": "Cesto ropa sucia bambú AG Box", "marketplace": "mercadolibre",
        "current_price": 406.0, "previous_price": None, "discount_percent": 59,
        "affiliate_url": "https://meli.la/x", "url": "https://meli.la/x",
        "image_url": "https://x/i.jpg",
    }
    out = pub._ml_price_gate(_item(payload))
    assert out is not None
    assert out.discard_reason == "ml_no_verified_previous_price"


# I2 — ML benchmark CON previous verificado pasa
def test_I2_ml_benchmark_verified_passes():
    pub = _pub()
    payload = {
        "title": "Cesto ropa sucia bambú AG Box", "marketplace": "mercadolibre",
        "current_price": 406.0, "previous_price": 999.0, "discount_percent": 59,
        "affiliate_url": "https://meli.la/x", "url": "https://meli.la/x",
        "image_url": "https://x/i.jpg",
        "ml_previous_price_verified": True, "current_price_verified": True,
        "discount_percent_verified": True,
    }
    assert pub._ml_price_gate(_item(payload)) is None


# J — gates no relajados: discount <50 sigue siendo responsabilidad upstream,
#     pero el gate ML exige previous verificado siempre
def test_J_gates_not_relaxed():
    pub = _pub()
    # ML sin afiliado -> sigue bloqueando
    payload = {
        "title": "x", "marketplace": "mercadolibre", "current_price": 100,
        "previous_price": 300, "discount_percent": 67, "image_url": "https://x/i.jpg",
        "url": "https://meli.la/x", "ml_previous_price_verified": True,
        "current_price_verified": True,
    }
    # extreme 67<70 ok, pero sin affiliate_url el affiliate_gate bloquea
    err = pub._mercadolibre_affiliate_gate(_item(payload))
    assert err == "missing_affiliate_url"


# nuevas seeds presentes en los archivos (si corre en el repo)
def test_G_seeds_files_have_new_entries():
    # localizar config/seeds relativo al repo
    here = Path(__file__).resolve()
    root = here
    for _ in range(8):
        root = root.parent
        if (root / "config" / "seeds").exists():
            break
    ml = root / "config" / "seeds" / "mercadolibre.json"
    amz = root / "config" / "seeds" / "amazon.json"
    if ml.exists() and amz.exists():
        ml_urls = json.loads(ml.read_text(encoding="utf-8"))
        amz_urls = json.loads(amz.read_text(encoding="utf-8"))
        assert any("ofertas/cocina" in u for u in ml_urls)
        assert any("cesto-ropa-sucia" in u for u in ml_urls)
        assert any("deals-collection-home-kitchen" in u for u in amz_urls)
        assert any("fuente+de+poder" in u for u in amz_urls)
