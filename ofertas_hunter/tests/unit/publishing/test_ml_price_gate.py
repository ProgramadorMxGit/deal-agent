"""Tests del gate de precio Mercado Libre (anti falsos positivos de descuento).

Cubre criterios H del spec (publisher gate) y casos relacionados:
- sin previous_price verificado -> bloquea ml_no_verified_previous_price
- unit price (por kilo) -> bloquea ml_unit_price_as_current_price
- mensualidad -> bloquea ml_installment_as_current_price
- variante mismatch -> bloquea ml_variant_mismatch
- discount mismatch -> bloquea ml_discount_mismatch
- extremo sin verificar -> bloquea ml_extreme_discount_unverified
- válido con previous verificado -> publica
- Amazon NO afectado por el gate ML
"""
from __future__ import annotations

import pytest

from ofertas_hunter.models import OutboxItem, OutboxState, OutboxType
from ofertas_hunter.publishing.whatsapp_publisher import WhatsAppPublisher


class _FakeResp:
    success = True
    dry_run = True
    raw = {}


class _FakeClient:
    dry_run = True
    async def send_media(self, *a, **k):
        return _FakeResp()


def _pub():
    return WhatsAppPublisher(
        _FakeClient(), "120363@g.us", enabled=True,
        mercadolibre_affiliate_required=True, amazon_affiliate_required=True,
        ml_extreme_discount_threshold=70.0,
    )


def _item(payload, type_=OutboxType.NORMAL.value):
    return OutboxItem(id=1, offer_id=1, type=type_, message_payload=payload,
                      state=OutboxState.PENDING.value)


def _ml_payload(**over):
    p = {
        "title": "Proteina Whey Hidrolizada Isolate Blu-e 2.2kg Chocolate",
        "marketplace": "mercadolibre",
        "current_price": 650.0,
        "previous_price": None,
        "discount_percent": 78.0,
        "affiliate_url": "https://meli.la/32TDYSu",
        "image_url": "https://x/img.jpg",
        "url": "https://meli.la/32TDYSu",
        "item_id": "MLM67461444",
    }
    p.update(over)
    return p


def _gate(pub, payload):
    """Devuelve el PublishOutcome del gate ML o None si pasa."""
    return pub._ml_price_gate(_item(payload))


# --- caso real Blu-e: previous None + discount 78 -> bloquea ---
def test_blue_false_positive_blocked():
    out = _gate(_pub(), _ml_payload())
    assert out is not None
    assert out.discard_reason == "ml_no_verified_previous_price"


# --- sin previous verificado (flag false) -> bloquea ---
def test_no_verified_previous_blocks():
    out = _gate(_pub(), _ml_payload(previous_price=1299.0, ml_previous_price_verified=False))
    assert out is not None
    assert out.discard_reason == "ml_no_verified_previous_price"


# --- unit price (por kilo) en raw text -> bloquea ---
def test_unit_price_per_kilo_blocks():
    out = _gate(_pub(), _ml_payload(
        previous_price=1299.0, ml_previous_price_verified=True,
        current_price_raw_text="$650 ($295.45 por kilo)",
        discount_percent=50,
    ))
    assert out is not None
    assert out.discard_reason == "ml_unit_price_as_current_price"


def test_unit_price_flag_blocks():
    out = _gate(_pub(), _ml_payload(
        previous_price=1299.0, ml_previous_price_verified=True,
        current_price_is_unit_price=True, discount_percent=50,
    ))
    assert out.discard_reason == "ml_unit_price_as_current_price"


# --- mensualidad -> bloquea ---
def test_installment_blocks():
    out = _gate(_pub(), _ml_payload(
        previous_price=1299.0, ml_previous_price_verified=True,
        current_price_raw_text="4 meses sin intereses de $162.50",
        discount_percent=50,
    ))
    assert out.discard_reason == "ml_installment_as_current_price"


# --- variante mismatch -> bloquea ---
def test_variant_mismatch_blocks():
    out = _gate(_pub(), _ml_payload(
        previous_price=1299.0, ml_previous_price_verified=True,
        ml_variant_mismatch=True, discount_percent=50,
    ))
    assert out.discard_reason == "ml_variant_mismatch"


# --- discount mismatch (declarado != calculado) -> bloquea ---
def test_discount_mismatch_blocks():
    # prev=1000 cur=650 -> 35% calculado; declarado 78 -> mismatch
    out = _gate(_pub(), _ml_payload(
        current_price=650.0, previous_price=1000.0,
        ml_previous_price_verified=True, discount_percent=78,
    ))
    assert out.discard_reason == "ml_discount_mismatch"


# --- extremo >=70 verificado pero sin extreme_verified -> bloquea ---
def test_extreme_unverified_blocks():
    # prev=2954.55 cur=650 -> 78% calculado, declarado 78 coincide, pero extremo
    out = _gate(_pub(), _ml_payload(
        current_price=650.0, previous_price=2954.55,
        ml_previous_price_verified=True, discount_percent=78,
    ))
    assert out is not None
    assert out.discard_reason == "ml_extreme_discount_unverified"


# --- válido: previous verificado, descuento 50, coherente -> pasa (None) ---
def test_valid_ml_offer_passes():
    out = _gate(_pub(), _ml_payload(
        current_price=650.0, previous_price=1300.0,
        ml_previous_price_verified=True, discount_percent=50,
    ))
    assert out is None


# --- previous from other variant flag -> bloquea ---
def test_previous_from_other_variant_blocks():
    out = _gate(_pub(), _ml_payload(
        current_price=650.0, previous_price=1300.0,
        ml_previous_price_verified=True, discount_percent=50,
        ml_previous_price_from_other_variant=True,
    ))
    assert out.discard_reason == "ml_previous_price_from_other_variant"


# --- Amazon NO afectado por el gate ML ---
def test_amazon_not_affected_by_ml_gate():
    amazon_payload = {
        "title": "JBL Tune 510BT", "marketplace": "amazon",
        "current_price": 388.0, "previous_price": 899.0, "discount_percent": 57,
        "affiliate_url": "https://amzn.to/x", "image_url": "https://x/i.jpg",
        "url": "https://amzn.to/x", "old_price_verified": True,
    }
    out = _pub()._ml_price_gate(_item(amazon_payload))
    assert out is None  # gate ML ignora Amazon


# --- price_error ML no exige previous (no muestra Antes) ---
def test_ml_price_error_not_gated():
    out = _pub()._ml_price_gate(_item(_ml_payload(), type_=OutboxType.PRICE_ERROR.value))
    assert out is None
