"""Tests del clasificador de URLs."""

from __future__ import annotations

import pytest

from ofertas_hunter.exploration.url_classifier import classify


@pytest.mark.parametrize(
    "url,marketplace,kind",
    [
        # Amazon
        ("https://www.amazon.com.mx/dp/B0CZ2FW5R8", "amazon", "product"),
        ("https://www.amazon.com.mx/Some-Title/dp/B0CXYZ1234/ref=x", "amazon", "product"),
        ("https://www.amazon.com.mx/gp/product/B0CXYZ1234", "amazon", "product"),
        ("https://www.amazon.com.mx/s?k=laptop", "amazon", "listing"),
        ("https://www.amazon.com.mx/deals", "amazon", "deals"),
        ("https://www.amazon.com.mx/gp/goldbox", "amazon", "deals"),
        ("https://www.amazon.com.mx/gp/help/customer", "amazon", "unknown"),
        ("https://www.amazon.com.mx/ap/signin", "amazon", "unknown"),
        # Mercado Libre
        ("https://articulo.mercadolibre.com.mx/MLM12345678", "mercadolibre", "product"),
        ("https://www.mercadolibre.com.mx/p/MLM98765432", "mercadolibre", "product"),
        ("https://www.mercadolibre.com.mx/ofertas", "mercadolibre", "deals"),
        ("https://listado.mercadolibre.com.mx/laptop", "mercadolibre", "listing"),
        ("https://www.mercadolibre.com.mx/c/electronica-audio-y-video", "mercadolibre", "category"),
        ("https://www.mercadolibre.com.mx/login", "mercadolibre", "unknown"),
        ("https://www.mercadolibre.com.mx/blog", "mercadolibre", "unknown"),
        ("https://www.mercadolibre.com.mx/ayuda", "mercadolibre", "unknown"),
        # Other
        ("https://www.walmart.com.mx/p/x", "other", "unknown"),
        ("", "other", "unknown"),
    ],
)
def test_classify(url, marketplace, kind):
    info = classify(url)
    assert info.marketplace == marketplace
    assert info.kind == kind


def test_amazon_product_score_higher_than_listing():
    a = classify("https://www.amazon.com.mx/dp/B0CXYZ1234")
    b = classify("https://www.amazon.com.mx/s?k=laptop")
    assert a.score > b.score


def test_ml_product_score_higher_than_listing():
    a = classify("https://articulo.mercadolibre.com.mx/MLM12345678")
    b = classify("https://listado.mercadolibre.com.mx/laptop")
    assert a.score > b.score


def test_ml_generic_listing_low_score():
    """Un listado de categoría genérica debe tener score bajo (1.0) para no
    desplazar a las páginas de ofertas en el frontier."""
    info = classify("https://listado.mercadolibre.com.mx/computacion/laptops")
    assert info.kind == "listing"
    assert info.score == 1.0


def test_ml_discount_listing_scores_high_as_deals():
    """Un listado con filtro de descuento es de ofertas → score alto."""
    info = classify("https://listado.mercadolibre.com.mx/freidora-de-aire_Descuento_50-100")
    assert info.kind == "deals"
    assert info.score >= 7.0


def test_ml_mas_vendidos_is_deals():
    info = classify("https://www.mercadolibre.com.mx/mas-vendidos/MLM1000")
    assert info.kind == "deals"


def test_ml_deal_listing_beats_generic_listing():
    deal = classify("https://listado.mercadolibre.com.mx/laptop_Descuento_50-100")
    generic = classify("https://listado.mercadolibre.com.mx/laptop")
    assert deal.score > generic.score
