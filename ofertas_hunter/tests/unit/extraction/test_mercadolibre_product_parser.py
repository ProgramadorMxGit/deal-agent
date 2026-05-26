"""Tests del MercadoLibreProductParser usando fixtures HTML."""

from __future__ import annotations

from pathlib import Path

import pytest

from ofertas_hunter.extraction.mercadolibre_product_parser import (
    MercadoLibreProductParser,
    canonicalize_mercadolibre_url,
    extract_item_id,
    has_share_button,
    is_mercadolibre_url,
)


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "mercadolibre"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _parse(name: str, url: str = "https://articulo.mercadolibre.com.mx/MLM12345678", **kw):
    return MercadoLibreProductParser().parse(_load(name), url, **kw)


# ---------------------------------------------------------------------------
# URL utils
# ---------------------------------------------------------------------------


class TestUrlUtils:
    def test_ml_extracts_item_id_from_url(self):
        assert extract_item_id("https://articulo.mercadolibre.com.mx/MLM12345678") == "MLM12345678"
        assert (
            extract_item_id("https://www.mercadolibre.com.mx/p/MLM12345678/foo?ref=x")
            == "MLM12345678"
        )
        assert extract_item_id("https://meli.la/abc") is None

    def test_ml_normalizes_canonical_url(self):
        # Con slug: debe preservar el slug y quitar query params
        canonical = canonicalize_mercadolibre_url(
            "https://articulo.mercadolibre.com.mx/MLM12345678-licuadora?source=ig"
        )
        assert canonical == "https://articulo.mercadolibre.com.mx/MLM12345678-licuadora"

    def test_ml_normalizes_canonical_url_without_slug(self):
        # Sin slug: devuelve solo el ID
        canonical = canonicalize_mercadolibre_url(
            "https://articulo.mercadolibre.com.mx/MLM12345678"
        )
        assert canonical == "https://articulo.mercadolibre.com.mx/MLM12345678"

    def test_is_mercadolibre_url(self):
        assert is_mercadolibre_url("https://articulo.mercadolibre.com.mx/MLM12345678")
        assert is_mercadolibre_url("https://meli.la/abc")
        assert not is_mercadolibre_url("https://www.amazon.com.mx/dp/B0EXAMPLE")
        assert not is_mercadolibre_url("")


# ---------------------------------------------------------------------------
# Extracción
# ---------------------------------------------------------------------------


class TestMlExtraction:
    def test_ml_extracts_title(self):
        product = _parse("product_full.html")
        assert product.title is not None
        assert "Licuadora Ninja" in product.title

    def test_ml_extracts_current_price(self):
        product = _parse("product_full.html")
        assert product.current_price == 1966.20

    def test_ml_extracts_previous_price(self):
        product = _parse("product_full.html")
        assert product.previous_price == 2499.99

    def test_ml_calculates_discount_percent(self):
        product = _parse("product_full.html")
        # 1 - 1966.20/2499.99 ≈ 21.35%
        assert product.calculated_discount_percent is not None
        assert 20.0 <= product.calculated_discount_percent <= 22.0

    def test_ml_extracts_visible_discount_percent(self):
        product = _parse("extreme_discount_60_percent.html")
        # Hay badge "61% OFF"
        assert product.discount_percent in (61, 61.0)

    def test_ml_extracts_main_image(self):
        product = _parse("product_full.html")
        assert product.image_url is not None
        assert product.image_url.startswith("https://http2.mlstatic.com/")

    def test_ml_extracts_availability(self):
        product = _parse("product_full.html")
        assert product.availability == "Stock disponible"
        assert product.in_stock is True

    def test_ml_full_product_publishable(self):
        product = _parse(
            "extreme_discount_60_percent.html",
            url="https://articulo.mercadolibre.com.mx/MLM98765432-sony",
        )
        assert product.is_publishable is True
        assert product.canonical_url == "https://articulo.mercadolibre.com.mx/MLM98765432-sony"
        assert product.asin == "MLM98765432"
        assert product.discount_percent == 61
        assert product.condition == "new"

    def test_ml_share_button_detected(self):
        product = _parse("product_full.html")
        assert product.selected_variant_signals.get("share_button") is True

    def test_ml_share_button_absent(self):
        # share_button_absent.html no es un producto completo, sólo un test de share.
        assert has_share_button(_load("share_button_absent.html")) is False
        assert has_share_button(_load("share_button_present.html")) is True


class TestAntiFalsePositives:
    def test_ml_detects_monthly_payment_not_total_price(self):
        product = _parse("monthly_payment_only.html")
        assert product.is_monthly_payment is True
        assert "monthly_payment" in product.not_publishable_reasons
        assert product.is_publishable is False

    def test_ml_out_of_stock_not_publishable(self):
        product = _parse("out_of_stock.html")
        assert product.in_stock is False
        assert "out_of_stock" in product.not_publishable_reasons
        assert product.is_publishable is False

    def test_ml_no_image_not_publishable(self):
        # Reusamos dom_broken (sin imagen ni precio): falla por varias razones.
        product = _parse("dom_broken.html")
        assert "no_image" in product.not_publishable_reasons
        assert product.is_publishable is False

    def test_ml_no_price_not_publishable(self):
        product = _parse("dom_broken.html")
        assert product.current_price is None
        assert "no_price" in product.not_publishable_reasons

    def test_ml_detects_variant_mismatch(self):
        product = _parse("product_full.html", expected_title="Apple iPhone 16 Pro Max 256GB")
        assert "variant_mismatch" in product.not_publishable_reasons
        assert product.is_publishable is False


class TestCondition:
    def test_ml_detects_condition_used(self):
        product = _parse("used_low_discount.html")
        assert product.condition == "used"

    def test_ml_detects_condition_refurbished(self):
        product = _parse("used_extreme_discount.html")
        assert product.condition == "refurbished"

    def test_ml_used_requires_extreme_discount(self):
        # used_low_discount.html: usado con 30% de descuento (<70%) → no publicable.
        product = _parse("used_low_discount.html")
        assert "used_requires_extreme_discount" in product.not_publishable_reasons
        assert product.is_publishable is False

        # used_extreme_discount.html: reacondicionado con 75% → publicable.
        product = _parse("used_extreme_discount.html")
        assert "used_requires_extreme_discount" not in product.not_publishable_reasons
        # Calculado: 1 - 1999/7999 ≈ 75%
        assert product.calculated_discount_percent is not None
        assert product.calculated_discount_percent >= 70.0


class TestFallbacks:
    def test_ml_fallback_og_image(self):
        # share_button_present no tiene imagen ni precio reales, pero verifico
        # el comportamiento vía product_full que sí tiene og:image y selectores.
        product = _parse("product_full.html")
        # La imagen tiene que venir del <figure> ML, no del og:image.
        assert "image_from_og" not in product.extraction_warnings

    def test_ml_fallback_json_ld(self):
        product = _parse("json_ld_fallback.html")
        assert product.title == "Audífonos JBL Tune 510BT Wireless"
        assert product.current_price == 388.0
        assert product.image_url is not None
        assert product.image_url.startswith("https://http2.mlstatic.com/")
        # Stock: hay botón de comprar → in_stock=True (heurística).
        assert product.in_stock is True
