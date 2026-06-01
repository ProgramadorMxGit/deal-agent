"""Tests del formateador de mensajes WhatsApp."""

from __future__ import annotations

import pytest

from ofertas_hunter.publishing.formatter import (
    FormatterError,
    format_normal_offer,
    format_price_error,
)


class TestNormalOfferFormatter:
    def test_jbl_example_matches_user_specified_format(self):
        """El ejemplo del usuario (JBL Tune 510BT) debe coincidir letra por letra
        con el formato corregido."""
        msg = format_normal_offer(
            title="JBL Tune 510BT - Auriculares in-Ear inalámbricos con Sonido Purebass, Color Azul",
            current_price=388.0,
            previous_price=899.0,
            discount_percent=57,
            url="https://amzn.to/4e3yTjG",
            image_url="https://m.media-amazon.com/images/I/x.jpg",
        )

        expected = (
            "*JBL Tune 510BT - Auriculares in-Ear inalámbricos con Sonido Purebass, Color Azul*\n"
            "\n"
            "🔥 *57% de descuento*\n"
            "❌ Antes: ~$899~\n"
            "✅ *AHORA: $388*\n"
            "\n"
            "👉 *Ver oferta:*\n"
            "https://amzn.to/4e3yTjG"
        )
        assert msg.text == expected
        assert msg.type == "normal"

    def test_decimal_price_preserved_when_not_integer(self):
        msg = format_normal_offer(
            title="Producto",
            current_price=388.5,
            previous_price=899.0,
            discount_percent=57,
            url="https://amzn.to/x",
            image_url="https://amazon.com/img.jpg",
        )
        assert "AHORA: $388.50" in msg.text
        assert "Antes: ~$899~" in msg.text

    def test_no_image_raises(self):
        with pytest.raises(FormatterError):
            format_normal_offer(
                title="Producto",
                current_price=100,
                previous_price=200,
                discount_percent=50,
                url="https://amzn.to/x",
                image_url="",
            )

    def test_invalid_url_raises(self):
        with pytest.raises(FormatterError):
            format_normal_offer(
                title="Producto",
                current_price=100,
                previous_price=200,
                discount_percent=50,
                url="not-a-url",
                image_url="https://amazon.com/img.jpg",
            )


class TestPriceErrorFormatter:
    def test_basic_price_error_format(self):
        msg = format_price_error(
            title="Apple iPhone 16 Pro Max 256GB",
            current_price=3899,
            confidence_label="very high",
            marketplace="liverpool",
            url="https://liverpool.com.mx/p/x",
            image_url="https://liverpool.com.mx/img.jpg",
        )
        assert "🚨 ERROR DE PRECIO 🚨" in msg.text
        assert "*Apple iPhone 16 Pro Max 256GB*" in msg.text
        assert "Precio detectado: *$3,899*" in msg.text
        assert "Confianza: very high" in msg.text
        assert "Tienda: Liverpool" in msg.text
        assert msg.type == "price_error"

    def test_confidence_label_validated(self):
        with pytest.raises(FormatterError):
            format_price_error(
                title="X",
                current_price=100,
                confidence_label="low",  # no permitido en publicación
                marketplace="amazon",
                url="https://x.com/p/x",
                image_url="https://x.com/img.jpg",
            )



# ---------------------------------------------------------------------------
# Coherencia precio↔descuento (bug del Kingston SD: badge 67% pero real 13%)
# ---------------------------------------------------------------------------


def test_formatter_corrects_inflated_discount_when_prices_dont_match():
    """Si el ``discount_percent`` reportado no cuadra con la diferencia
    real entre ``previous_price`` y ``current_price``, el formatter
    recalcula y usa el valor real.
    """
    from ofertas_hunter.publishing.formatter import format_normal_offer

    msg = format_normal_offer(
        title="Kingston Canvas Select Plus 256GB",
        current_price=780.0,
        previous_price=899.0,
        discount_percent=67.0,  # mal extraído (real es 13%)
        url="https://amzn.to/x",
        image_url="https://m.media-amazon.com/img.jpg",
    )
    # Usa cálculo real: (899 - 780) / 899 ≈ 13.2% → "13%"
    assert "13%" in msg.text
    assert "67%" not in msg.text
    assert "$899" in msg.text
    assert "$780" in msg.text


def test_formatter_preserves_discount_when_prices_match():
    from ofertas_hunter.publishing.formatter import format_normal_offer

    msg = format_normal_offer(
        title="JBL Tune 510BT",
        current_price=388.0,
        previous_price=899.0,
        discount_percent=57.0,  # cuadra con el cálculo real (56.84%)
        url="https://amzn.to/x",
        image_url="https://m.media-amazon.com/img.jpg",
    )
    assert "57%" in msg.text


def test_formatter_rejects_when_previous_price_is_missing():
    from ofertas_hunter.publishing.formatter import (
        FormatterError,
        format_normal_offer,
    )

    with pytest.raises(FormatterError, match="previous_price"):
        format_normal_offer(
            title="X",
            current_price=100.0,
            previous_price=None,  # sin precio anterior
            discount_percent=50.0,
            url="https://x",
            image_url="https://m.media-amazon.com/img.jpg",
        )


def test_formatter_rejects_when_previous_lte_current():
    from ofertas_hunter.publishing.formatter import (
        FormatterError,
        format_normal_offer,
    )

    with pytest.raises(FormatterError, match="must be <"):
        format_normal_offer(
            title="X",
            current_price=100.0,
            previous_price=100.0,
            discount_percent=50.0,
            url="https://x",
            image_url="https://m.media-amazon.com/img.jpg",
        )
