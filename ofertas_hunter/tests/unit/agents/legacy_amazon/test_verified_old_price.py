"""Tests del extractor estricto de precio anterior verificado de Amazon.

`extract_verified_old_price(html) -> (old_price, source)`:
- Solo acepta precio tachado/list-price dentro del bloque principal de precio.
- Rechaza precios de variantes, otros vendedores y mensualidades/MSI.
- Si no hay precio anterior confiable → (None, None).
"""

from __future__ import annotations

from ofertas_hunter.agents.legacy_amazon.price_parser import (
    extract_verified_old_price,
)


def test_no_strike_price_returns_none():
    """Caso real sudadera $350: solo precio actual, sin precio anterior visible."""
    html = """
    <div id="corePriceDisplay_desktop_feature_div">
      <span class="a-price priceToPay"><span class="a-offscreen">$350.00</span></span>
    </div>
    """
    old, source = extract_verified_old_price(html)
    assert old is None
    assert source is None


def test_valid_basis_strike_price():
    """Precio de lista tachado visible dentro del bloque principal."""
    html = """
    <div id="corePriceDisplay_desktop_feature_div">
      <span class="a-price priceToPay"><span class="a-offscreen">$350.00</span></span>
      <span class="a-price a-text-price basisPrice" data-a-strike="true">
        <span class="a-offscreen">$800.00</span>
      </span>
    </div>
    """
    old, source = extract_verified_old_price(html)
    assert old == 800.0
    assert source is not None


def test_variant_prices_not_used_as_old_price():
    """Producto seleccionado sin precio anterior; variantes con precios mayores.

    Los precios de variantes (twister) NO deben usarse como precio anterior.
    """
    html = """
    <div id="corePriceDisplay_desktop_feature_div">
      <span class="a-price priceToPay"><span class="a-offscreen">$350.00</span></span>
    </div>
    <div id="twister">
      <span class="a-price a-text-price"><span class="a-offscreen">$468.00</span></span>
      <span class="a-price a-text-price"><span class="a-offscreen">$520.00</span></span>
    </div>
    """
    old, source = extract_verified_old_price(html)
    assert old is None
    assert source is None


def test_monthly_payment_not_used_as_old_price():
    """Mensualidad / MSI no es precio anterior."""
    html = """
    <div id="corePriceDisplay_desktop_feature_div">
      <span class="a-price priceToPay"><span class="a-offscreen">$350.00</span></span>
    </div>
    <div id="mir-layout-DELIVERY_BLOCK">
      <span class="a-text-price"><span class="a-offscreen">$35.52</span></span> x 12 meses
    </div>
    """
    old, source = extract_verified_old_price(html)
    assert old is None
    assert source is None


def test_other_sellers_price_not_used_as_old_price():
    """Precios de 'otros vendedores' (aod) no son precio anterior del producto."""
    html = """
    <div id="corePriceDisplay_desktop_feature_div">
      <span class="a-price priceToPay"><span class="a-offscreen">$350.00</span></span>
    </div>
    <div id="aod-offer-list">
      <span class="a-price a-text-price"><span class="a-offscreen">$900.00</span></span>
    </div>
    """
    old, source = extract_verified_old_price(html)
    assert old is None
    assert source is None


def test_strike_must_be_greater_than_current_is_caller_concern():
    """El extractor devuelve el strike aunque sea menor; la verificación de
    prev>cur la hace el adapter/gate. Aquí solo confirmamos que extrae el
    valor del bloque principal."""
    html = """
    <div id="apex_offerDisplay_desktop">
      <span class="a-price priceToPay"><span class="a-offscreen">$350.00</span></span>
      <span class="basisPrice"><span class="a-price a-text-price">
        <span class="a-offscreen">$1,200.00</span></span></span>
    </div>
    """
    old, source = extract_verified_old_price(html)
    assert old == 1200.0
    assert source is not None


def test_empty_html_returns_none():
    old, source = extract_verified_old_price("")
    assert old is None
    assert source is None
