"""Tests de extractores de URLs desde listings/categorías."""

from __future__ import annotations

from ofertas_hunter.exploration.listing_extractor import (
    extract_amazon_deals,
    extract_amazon_listing,
    extract_mercadolibre_listing,
)


def test_amazon_listing_extracts_products_from_data_asin():
    html = """
<html><body>
<div data-component-type="s-search-result" data-asin="B0EXAMPLE1"></div>
<div data-component-type="s-search-result" data-asin="B0EXAMPLE2"></div>
<a class="s-pagination-next" href="/s?k=laptop&page=2">Next</a>
</body></html>
"""
    items = extract_amazon_listing(html)
    products = [i for i in items if i.kind == "product"]
    listings = [i for i in items if i.kind == "listing"]
    assert len(products) == 2
    assert {p.url for p in products} == {
        "https://www.amazon.com.mx/dp/B0EXAMPLE1",
        "https://www.amazon.com.mx/dp/B0EXAMPLE2",
    }
    assert len(listings) >= 1


def test_amazon_deals_extracts_high_score_products():
    html = """
<html><body>
<a href="/dp/B0DEAL1234">deal 1</a>
<a href="/dp/B0DEAL5678">deal 2</a>
</body></html>
"""
    items = extract_amazon_deals(html)
    assert len(items) == 2
    assert all(i.kind == "product" for i in items)
    # Productos en /deals tienen score más alto que en listings normales.
    assert all(i.score >= 10 for i in items)


def test_amazon_listing_dedupes():
    html = """
<html><body>
<div data-component-type="s-search-result" data-asin="B0EXAMPLE1"></div>
<a href="/dp/B0EXAMPLE1">duplicado</a>
</body></html>
"""
    items = extract_amazon_listing(html)
    products = [i for i in items if i.kind == "product"]
    assert len(products) == 1


def test_mercadolibre_listing_extracts_product_links():
    html = """
<html><body>
<a class="ui-search-link" href="/p/MLM12345678">producto 1</a>
<a class="ui-search-item__group__element" href="https://articulo.mercadolibre.com.mx/MLM98765432-foo">producto 2</a>
<a class="andes-pagination__link" href="/c/electronica?page=2">next</a>
</body></html>
"""
    items = extract_mercadolibre_listing(html, base_url="https://www.mercadolibre.com.mx/c/electronica")
    products = [i for i in items if i.kind == "product"]
    assert len(products) == 2
    # Paginación a otra categoría queda como `category` (URL normalizada).
    pagin = [i for i in items if i.kind in ("listing", "category")]
    assert len(pagin) >= 1


def test_extractors_handle_empty_html():
    assert extract_amazon_listing("") == []
    assert extract_amazon_deals("") == []
    assert extract_mercadolibre_listing("") == []


def test_amazon_listing_normalizes_relative_urls():
    html = '<div data-component-type="s-search-result"><h2><a href="/dp/B0CXYZ1234?ref=x">x</a></h2></div>'
    items = extract_amazon_listing(html)
    assert items[0].url.startswith("https://www.amazon.com.mx/dp/B0CXYZ1234")
