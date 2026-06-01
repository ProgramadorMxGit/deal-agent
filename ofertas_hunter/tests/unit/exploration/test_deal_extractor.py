"""Tests del extractor mejorado de deal/listing pages (EXTRACTOR_V2)."""
from __future__ import annotations

from ofertas_hunter.exploration.listing_extractor import (
    extract_amazon_deals, extract_amazon_listing,
    extract_mercadolibre_listing, extract_mercadolibre_listing_items,
)
from ofertas_hunter.exploration.curated_reseed import is_curated_seed, load_curated_seeds


# F/G — Amazon deal grid con data-asin
def test_amazon_deals_data_asin():
    html = """
    <div class="deals-grid">
      <div data-asin="B09RCQ5NFY"><span>Olla Lamex</span></div>
      <div data-asin="B0CWLTDQ5K"><span>Corsair RM750x</span></div>
      <div data-asin="B0FCQG6LNC"><span>Stanley Quencher</span></div>
    </div>"""
    urls = extract_amazon_deals(html)
    found = {u.url for u in urls}
    assert any("B09RCQ5NFY" in u for u in found)
    assert any("B0CWLTDQ5K" in u for u in found)
    assert any("B0FCQG6LNC" in u for u in found)
    assert all(u.kind == "product" for u in urls)


# I — card con /dp/ASIN directo
def test_amazon_deals_dp_links():
    html = '<a href="/dp/B09RCQ5NFY?tag=x">Olla</a> <a href="https://www.amazon.com.mx/dp/B0FCQG6LNC">Stanley</a>'
    urls = extract_amazon_deals(html)
    assert any("B09RCQ5NFY" in u.url for u in urls)
    assert any("B0FCQG6LNC" in u.url for u in urls)


# H — search result con descuento (data-asin)
def test_amazon_search_cards():
    html = """
    <div data-component-type="s-search-result" data-asin="B09RCQ5NFY"><h2><a href="/dp/B09RCQ5NFY">x</a></h2></div>
    """
    urls = extract_amazon_listing(html)
    assert any("B09RCQ5NFY" in u.url for u in urls)


# A/B — ML poly-card (layout nuevo de /ofertas)
def test_ml_poly_card_extraction():
    html = """
    <div class="poly-card">
      <a class="poly-component__title" href="https://www.mercadolibre.com.mx/p/MLM12345678">Cesto bambú</a>
      <span class="andes-money-amount andes-money-amount--previous"><span class="andes-money-amount__fraction">999</span></span>
      <span class="andes-money-amount"><span class="andes-money-amount__fraction">406</span></span>
      <span class="andes-money-amount__discount">59% OFF</span>
    </div>"""
    items = extract_mercadolibre_listing_items(html)
    assert len(items) == 1
    assert "MLM12345678" in items[0].url
    assert items[0].current_price == 406.0
    assert items[0].original_price == 999.0
    assert items[0].discount_percent == 59.0


# D — card con link MLM (sin /p/)
def test_ml_listing_link_extraction():
    html = '<a class="poly-component__title" href="https://articulo.mercadolibre.com.mx/MLM45918752-x">prod</a>'
    urls = extract_mercadolibre_listing(html)
    assert any("MLM45918752" in u.url for u in urls)


# no cards -> diagnóstico claro (lista vacía, sin error)
def test_no_cards_returns_empty():
    assert extract_mercadolibre_listing_items("<html><body>nada</body></html>") == []
    assert extract_amazon_deals("<html></html>") == []


# precios de listing NO se marcan verificados (solo discovery)
def test_listing_prices_not_verified():
    html = """
    <div class="poly-card">
      <a class="poly-component__title" href="https://www.mercadolibre.com.mx/p/MLM999">x</a>
      <span class="andes-money-amount"><span class="andes-money-amount__fraction">500</span></span>
    </div>"""
    items = extract_mercadolibre_listing_items(html)
    # MercadoLibreListingItem no tiene flag de verificación: el gate revalida en PDP
    assert not hasattr(items[0], "ml_previous_price_verified")


# curated seed detection
def test_is_curated_seed():
    assert is_curated_seed("https://www.mercadolibre.com.mx/ofertas/cocina")
    assert is_curated_seed("https://listado.mercadolibre.com.mx/olla_Descuento_50-100")
    assert is_curated_seed("https://www.amazon.com.mx/deals?bubble-id=deals-collection-home-kitchen")
    assert is_curated_seed("https://www.amazon.com.mx/s?k=x&rh=p_n_pct-off-with-tax%3A50-")
    assert not is_curated_seed("https://www.amazon.com.mx/gp/bestsellers/")
    assert not is_curated_seed("https://www.mercadolibre.com.mx/c/computacion")
