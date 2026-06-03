"""Tests del mapeo categoría -> URL de ofertas ML (mantener diversidad sobre
ofertas reales)."""

from ofertas_hunter.exploration.category_offer_seeds import (
    ml_offers_url_for_category,
    ml_offers_urls_for_categories,
)
from ofertas_hunter.exploration.url_classifier import classify


def test_known_category_maps_to_offers_slug():
    url = ml_offers_url_for_category("tecnologia")
    assert url == "https://www.mercadolibre.com.mx/ofertas/tecnologia"


def test_unknown_category_falls_back_to_offers_hub():
    url = ml_offers_url_for_category("categoria-rara")
    assert url == "https://www.mercadolibre.com.mx/ofertas"


def test_mapped_urls_classify_as_deals():
    """Las URLs sembradas deben clasificar como 'deals' (no listing genérico)."""
    for cat in ("tecnologia", "hogar", "bebe", "belleza"):
        info = classify(ml_offers_url_for_category(cat))
        assert info.marketplace == "mercadolibre"
        assert info.kind == "deals", f"{cat} -> {info.kind}"


def test_urls_for_categories_dedup_and_order():
    cats = ["tecnologia", "ropa", "calzado", "tecnologia"]
    urls = ml_offers_urls_for_categories(cats)
    # ropa y calzado mapean ambas a /moda -> se deduplica
    assert urls[0] == "https://www.mercadolibre.com.mx/ofertas/tecnologia"
    assert "https://www.mercadolibre.com.mx/ofertas/moda" in urls
    assert len(urls) == len(set(urls))
