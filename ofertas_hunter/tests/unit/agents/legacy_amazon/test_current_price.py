"""Regression tests for Amazon current-price extraction."""

from __future__ import annotations

from ofertas_hunter.agents.legacy_amazon.price_parser import (
    extract_verified_current_price,
)


def test_current_price_ignores_unit_price_in_same_price_block():
    """Amazon can show `$148 ($0.74 / unidad)` in the price block.

    The publishable current price is the pack price, not the per-unit price.
    """

    html = """
    <div id="corePriceDisplay_desktop_feature_div">
      <span class="a-price aok-align-center reinventPricePriceToPayMargin priceToPay">
        <span class="a-offscreen">$148.00</span>
        <span aria-hidden="true">
          <span class="a-price-whole">148</span><span class="a-price-fraction">00</span>
        </span>
      </span>
      <span class="a-size-base a-color-secondary">
        (<span class="a-price a-text-price"><span class="a-offscreen">$0.74</span></span> / unidad)
      </span>
      <span class="basisPrice">
        <span class="a-price a-text-price" data-a-strike="true">
          <span class="a-offscreen">$222.00</span>
        </span>
      </span>
    </div>
    """

    price, source = extract_verified_current_price(html)

    assert price == 148.0
    assert source == "core_price_display_price_to_pay"


def test_current_price_rejects_unit_price_when_it_is_the_only_candidate():
    html = """
    <div id="corePriceDisplay_desktop_feature_div">
      <span class="a-size-base a-color-secondary">
        (<span class="a-price a-text-price"><span class="a-offscreen">$0.74</span></span> / unidad)
      </span>
    </div>
    """

    price, source = extract_verified_current_price(html)

    assert price is None
    assert source is None
