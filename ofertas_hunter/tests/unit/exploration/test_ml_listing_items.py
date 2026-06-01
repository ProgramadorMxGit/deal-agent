"""Tests del extractor a nivel de tarjeta para Mercado Libre.

`extract_mercadolibre_listing_items()` lee las cards de un listing ML y
devuelve `MercadoLibreListingItem` con metadata de precio/descuento cuando
está visible, para permitir un prefiltro temprano antes de Playwright.
"""

from __future__ import annotations

from ofertas_hunter.exploration.listing_extractor import (
    MercadoLibreListingItem,
    extract_mercadolibre_listing_items,
)


def _card(href: str, *, title: str = "", inner: str = "") -> str:
    return f"""
    <li class="ui-search-layout__item">
      <div class="ui-search-result__wrapper">
        <a class="ui-search-link" href="{href}" title="{title}">{title}</a>
        {inner}
      </div>
    </li>
    """


def test_item_with_explicit_off_text_parses_discount():
    inner = """
      <span class="andes-money-amount andes-money-amount--previous">
        <span class="andes-money-amount__fraction">1,000</span>
      </span>
      <span class="andes-money-amount andes-money-amount--cents-superscript">
        <span class="andes-money-amount__fraction">500</span>
      </span>
      <span class="andes-money-amount__discount">50% OFF</span>
    """
    html = f"<ul>{_card('/p/MLM11111111', title='Producto A', inner=inner)}</ul>"
    items = extract_mercadolibre_listing_items(html)
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, MercadoLibreListingItem)
    assert item.url == "https://www.mercadolibre.com.mx/p/MLM11111111"
    assert item.kind == "product"
    assert item.discount_percent == 50.0
    assert item.raw_discount_text and "50" in item.raw_discount_text


def test_item_with_49_off_parses_below_threshold():
    inner = '<span class="andes-money-amount__discount">49% OFF</span>'
    html = f"<ul>{_card('/p/MLM22222222', inner=inner)}</ul>"
    items = extract_mercadolibre_listing_items(html)
    assert len(items) == 1
    assert items[0].discount_percent == 49.0


def test_item_without_discount_is_unknown():
    inner = """
      <span class="andes-money-amount andes-money-amount--cents-superscript">
        <span class="andes-money-amount__fraction">799</span>
      </span>
    """
    html = f"<ul>{_card('/p/MLM33333333', inner=inner)}</ul>"
    items = extract_mercadolibre_listing_items(html)
    assert len(items) == 1
    assert items[0].discount_percent is None
    assert items[0].raw_discount_text is None


def test_item_calculates_discount_from_prices_when_no_off_text():
    """Sin texto '% OFF' pero con precio actual + anterior → calcula descuento."""
    inner = """
      <s class="andes-money-amount andes-money-amount--previous">
        <span class="andes-money-amount__fraction">1,000</span>
      </s>
      <span class="andes-money-amount andes-money-amount--cents-superscript ui-search-price__part">
        <span class="andes-money-amount__fraction">400</span>
      </span>
    """
    html = f"<ul>{_card('/p/MLM44444444', inner=inner)}</ul>"
    items = extract_mercadolibre_listing_items(html)
    assert len(items) == 1
    item = items[0]
    assert item.current_price == 400.0
    assert item.original_price == 1000.0
    # (1 - 400/1000) * 100 = 60
    assert item.discount_percent == 60.0


def test_item_normalizes_relative_product_url():
    html = f"<ul>{_card('/MLM55555555-zapatos-foo')}</ul>"
    items = extract_mercadolibre_listing_items(html)
    assert len(items) == 1
    assert items[0].url.startswith("https://www.mercadolibre.com.mx/MLM55555555")


def test_empty_html_returns_empty_list():
    assert extract_mercadolibre_listing_items("") == []
