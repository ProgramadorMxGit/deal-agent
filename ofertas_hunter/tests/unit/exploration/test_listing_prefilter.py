"""Tests del prefiltro temprano de descuento para items de listing ML."""

from __future__ import annotations

from ofertas_hunter.exploration.listing_extractor import MercadoLibreListingItem
from ofertas_hunter.exploration.listing_prefilter import (
    PrefilterDecision,
    decide_listing_item,
)


def _item(discount):
    return MercadoLibreListingItem(
        url="https://www.mercadolibre.com.mx/p/MLM00000001",
        kind="product",
        discount_percent=discount,
    )


def test_discount_at_threshold_is_accepted():
    decision = decide_listing_item(_item(50.0), min_discount=50.0)
    assert decision.accept is True
    assert decision.bucket == "accepted_discount"


def test_discount_above_threshold_is_accepted():
    decision = decide_listing_item(_item(60.0), min_discount=50.0)
    assert decision.accept is True
    assert decision.bucket == "accepted_discount"


def test_discount_below_threshold_is_rejected():
    decision = decide_listing_item(_item(49.0), min_discount=50.0)
    assert decision.accept is False
    assert decision.bucket == "discarded_low_discount"


def test_unknown_discount_lax_accepts_with_low_score():
    decision = decide_listing_item(
        _item(None), min_discount=50.0, strict=False, unknown_score=1.0
    )
    assert decision.accept is True
    assert decision.bucket == "accepted_unknown"
    assert decision.score == 1.0


def test_unknown_discount_strict_rejects():
    decision = decide_listing_item(_item(None), min_discount=50.0, strict=True)
    assert decision.accept is False
    assert decision.bucket == "discarded_unknown_strict"


def test_non_product_kind_is_accepted_as_navigational():
    """listing/category siguen entrando (navegación), no se filtran por precio."""
    item = MercadoLibreListingItem(
        url="https://www.mercadolibre.com.mx/c/electronica",
        kind="category",
        discount_percent=None,
    )
    decision = decide_listing_item(item, min_discount=50.0, strict=True)
    assert decision.accept is True
    assert decision.bucket == "accepted_navigational"


def test_decision_is_dataclass_with_expected_fields():
    decision = decide_listing_item(_item(55.0), min_discount=50.0)
    assert isinstance(decision, PrefilterDecision)
    assert hasattr(decision, "accept")
    assert hasattr(decision, "bucket")
    assert hasattr(decision, "score")
