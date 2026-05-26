"""Tests del AmazonProductParser usando fixtures HTML."""

from __future__ import annotations

from pathlib import Path

import pytest

from ofertas_hunter.extraction.amazon_product_parser import AmazonProductParser
from ofertas_hunter.marketplaces.url_utils import (
    canonicalize_amazon_url,
    extract_asin,
    is_amazon_url,
)


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "amazon"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _parse(name: str, url: str = "https://www.amazon.com.mx/dp/B0EXAMPLE", **kwargs):
    return AmazonProductParser().parse(_load(name), url, **kwargs)


# ---------------------------------------------------------------------------
# URL utils
# ---------------------------------------------------------------------------


class TestUrlUtils:
    def test_amazon_extracts_asin_from_url(self):
        assert extract_asin("https://www.amazon.com.mx/dp/B0EXAMPLE1") == "B0EXAMPLE1"
        assert extract_asin("https://www.amazon.com.mx/dp/B0CXYZ1234/ref=blah") == "B0CXYZ1234"
        assert (
            extract_asin("https://www.amazon.com.mx/gp/product/B0CXYZ1234?tag=foo")
            == "B0CXYZ1234"
        )
        assert extract_asin("https://amzn.to/4abcdef") is None
        # No matchea con 11 caracteres (B0EXAMPLE12 tiene 11 → debe ser None
        # porque ASIN requiere exactamente 10).
        # En la práctica Amazon URLs ponen el ASIN seguido de '/' o '?'.

    def test_amazon_normalizes_canonical_url(self):
        canonical = canonicalize_amazon_url(
            "https://www.amazon.com.mx/Some-Title/dp/B0CXYZ1234/ref=sr_1_1?keywords=x"
        )
        assert canonical == "https://www.amazon.com.mx/dp/B0CXYZ1234"

    def test_is_amazon_url(self):
        assert is_amazon_url("https://www.amazon.com.mx/dp/B0CXYZ1234")
        assert is_amazon_url("https://amzn.to/4abcdef")
        assert not is_amazon_url("https://www.mercadolibre.com.mx/p/MLM12345")
        assert not is_amazon_url("")


# ---------------------------------------------------------------------------
# Parser principal
# ---------------------------------------------------------------------------


class TestAmazonExtraction:
    def test_amazon_extracts_title(self):
        product = _parse("jbl_normal_offer.html")
        assert product.title is not None
        assert "JBL Tune 510BT" in product.title

    def test_amazon_extracts_current_price(self):
        product = _parse("jbl_normal_offer.html")
        assert product.current_price == 388.0
        assert product.raw_price_text == "$388.00"

    def test_amazon_extracts_previous_price(self):
        product = _parse("jbl_normal_offer.html")
        assert product.previous_price == 899.0

    def test_amazon_calculates_discount_percent(self):
        product = _parse("jbl_normal_offer.html")
        # Hay badge -57%, y también cálculo (57.51%).
        assert product.discount_percent in (57, 57.0, 57.51)
        assert product.calculated_discount_percent is not None
        # 1 - 388/899 ≈ 56.84%
        assert 56.0 <= product.calculated_discount_percent <= 58.0

    def test_amazon_extracts_main_image(self):
        product = _parse("jbl_normal_offer.html")
        assert product.image_url is not None
        assert product.image_url.startswith("https://m.media-amazon.com/")

    def test_amazon_extracts_availability(self):
        product = _parse("jbl_normal_offer.html")
        assert product.availability is not None
        assert "stock" in (product.availability or "").lower()
        assert product.in_stock is True

    def test_amazon_jbl_is_publishable(self):
        product = _parse(
            "jbl_normal_offer.html",
            url="https://www.amazon.com.mx/Some-Title/dp/B0EXAMPLEK/ref=sr_1_1",
        )
        assert product.is_publishable is True
        assert product.canonical_url == "https://www.amazon.com.mx/dp/B0EXAMPLEK"
        assert product.asin == "B0EXAMPLEK"


class TestAntiFalsePositives:
    def test_amazon_detects_monthly_payment_not_total_price(self):
        product = _parse("monthly_payment_only.html")
        assert product.is_monthly_payment is True
        assert "monthly_payment" in product.not_publishable_reasons
        assert product.is_publishable is False

    def test_amazon_out_of_stock_not_publishable(self):
        product = _parse("out_of_stock.html")
        assert product.in_stock is False
        assert "out_of_stock" in product.not_publishable_reasons
        assert product.is_publishable is False

    def test_amazon_no_image_not_publishable(self):
        product = _parse("no_image.html")
        assert product.image_url is None
        assert "no_image" in product.not_publishable_reasons
        assert product.is_publishable is False

    def test_amazon_no_price_not_publishable(self):
        product = _parse("dom_broken.html")
        assert product.current_price is None
        assert "no_price" in product.not_publishable_reasons
        assert product.is_publishable is False

    def test_amazon_detects_variant_mismatch(self):
        # Pasamos un expected_title que no matchea con el producto real.
        product = _parse(
            "jbl_normal_offer.html",
            expected_title="Apple iPhone 16 Pro Max 256GB",
        )
        assert "variant_mismatch" in product.not_publishable_reasons
        assert product.is_publishable is False

    def test_amazon_dom_broken_has_warnings(self):
        product = _parse("dom_broken.html")
        assert product.is_publishable is False
        # Debería tener varias razones (no_title viene además de no_image/no_price)
        assert "no_title" in product.not_publishable_reasons
        assert "no_price" in product.not_publishable_reasons
        assert "no_image" in product.not_publishable_reasons


class TestFallbacks:
    def test_amazon_fallback_og_image(self):
        product = _parse("og_image_fallback.html")
        assert product.image_url is not None
        assert "og-fallback" in product.image_url
        assert "image_from_og" in product.extraction_warnings

    def test_amazon_fallback_json_ld(self):
        # Fixture sin selectores normales, sólo JSON-LD.
        product = _parse("json_ld_fallback.html")
        assert product.title == "Apple AirPods Pro 2da Generación"
        assert product.current_price == 4499.0
        assert product.image_url is not None
        assert product.image_url.startswith("https://m.media-amazon.com")
        # El stock se infiere por el botón add-to-cart aunque no haya
        # disponibilidad textual.
        assert product.in_stock is True


class TestExtremeCases:
    def test_amazon_iphone_extreme_low_price_extracted_correctly(self):
        product = _parse(
            "iphone_extreme_low_price.html",
            url="https://www.amazon.com.mx/iPhone-16-Pro-Max/dp/B0EXAMPLEI",
        )
        assert product.current_price == 3899.0
        assert product.previous_price == 32999.0
        assert product.discount_percent == 88
        assert product.in_stock is True
        assert product.title.startswith("Apple iPhone 16 Pro Max")
        assert product.is_publishable is True
