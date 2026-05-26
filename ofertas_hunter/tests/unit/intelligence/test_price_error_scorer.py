"""Tests del PriceErrorScorer.

Cubre los criterios obligatorios de la spec §16.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ofertas_hunter.intelligence.price_error_scorer import PriceErrorScorer
from ofertas_hunter.models import Classification, Condition, PriceErrorSignal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signal_from_fixture(data: dict) -> PriceErrorSignal:
    """Convierte un fixture JSON al modelo PriceErrorSignal."""
    return PriceErrorSignal(
        product_title=data["product_title"],
        marketplace=data["marketplace"],
        source=data["source"],
        original_url=data["original_url"],
        source_channel=data.get("source_channel"),
        original_text=data.get("original_text"),
        resolved_url=data.get("resolved_url"),
        current_price=data.get("current_price"),
        previous_price=data.get("previous_price"),
        historical_median_price=data.get("historical_median_price"),
        discount_percent=data.get("discount_percent"),
        brand=data.get("brand"),
        category=data.get("category"),
        condition=data.get("condition", "new"),
        has_stock=data.get("has_stock", True),
        has_image=data.get("has_image", True),
        urgency_terms=list(data.get("urgency_terms") or []),
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Tests obligatorios (spec §16)
# ---------------------------------------------------------------------------


class TestMandatoryScorerCases:
    """Tests con los nombres exigidos en la spec §16."""

    def test_price_error_laptop_extreme_low_price(self, load_price_error_example):
        """Ejemplo B: laptop combo $305.88 => price_error_confirmed very high."""
        fixture = load_price_error_example("B")
        signal = _signal_from_fixture(fixture)
        result = PriceErrorScorer().score(signal)

        assert result.classification == Classification.PRICE_ERROR_CONFIRMED.value
        assert result.score >= fixture["score_expected_min"], (
            f"score={result.score} < min={fixture['score_expected_min']} "
            f"reasons={result.reasons}"
        )
        assert result.confidence_label in ("very high", "high")
        assert result.is_publishable is True

    def test_price_error_iphone_extreme_low_price(self, load_price_error_example):
        """Ejemplo I: iPhone 16 Pro Max 256GB a $3,899 => very high."""
        fixture = load_price_error_example("I")
        signal = _signal_from_fixture(fixture)
        result = PriceErrorScorer().score(signal)

        assert result.classification == Classification.PRICE_ERROR_CONFIRMED.value
        assert result.score >= fixture["score_expected_min"]
        assert result.confidence_label == "very high"

    def test_price_error_samsung_flagship_extreme_low_price(self, load_price_error_example):
        """Ejemplo K: Galaxy S24 256GB a $1,399 => very high."""
        fixture = load_price_error_example("K")
        signal = _signal_from_fixture(fixture)
        result = PriceErrorScorer().score(signal)

        assert result.classification == Classification.PRICE_ERROR_CONFIRMED.value
        assert result.score >= fixture["score_expected_min"]
        assert result.confidence_label == "very high"

    def test_price_error_airpods_low_price(self, load_price_error_example):
        """Ejemplo H: AirPods Pro a $599.90 => price_error_confirmed."""
        fixture = load_price_error_example("H")
        signal = _signal_from_fixture(fixture)
        result = PriceErrorScorer().score(signal)

        assert result.classification == Classification.PRICE_ERROR_CONFIRMED.value
        assert result.score >= fixture["score_expected_min"]

    def test_price_error_visible_91_percent_discount(self, load_price_error_example):
        """Ejemplo D: 91% off explícito => al menos possible_price_error."""
        fixture = load_price_error_example("D")
        signal = _signal_from_fixture(fixture)
        result = PriceErrorScorer().score(signal)

        assert result.score >= fixture["score_expected_min"]
        assert result.classification in (
            Classification.PRICE_ERROR_CONFIRMED.value,
            Classification.POSSIBLE_PRICE_ERROR.value,
        )

    def test_telegram_urgency_terms_increase_score(self):
        """Texto Telegram con 'ERROR DE PRECIO' + 'CORRAN' debe sumar al score."""
        base = PriceErrorSignal(
            product_title="Producto random",
            marketplace="walmart",
            source="telegram",
            original_url="https://walmart.com.mx/p/x",
            current_price=999.0,
            has_image=True,
            has_stock=True,
        )
        without_urgency = base
        with_urgency = PriceErrorSignal(
            product_title="Producto random",
            marketplace="walmart",
            source="telegram",
            original_url="https://walmart.com.mx/p/x",
            original_text="🚨🔥 ERROR DE PRECIO 🚨 CORRAN A SOLO 999!!!",
            current_price=999.0,
            has_image=True,
            has_stock=True,
        )
        scorer = PriceErrorScorer()
        score_without = scorer.score(without_urgency).score
        score_with = scorer.score(with_urgency).score
        assert score_with > score_without

    def test_no_image_not_publishable(self):
        """Producto sin imagen NUNCA publicable."""
        signal = PriceErrorSignal(
            product_title="Apple AirPods Pro",
            marketplace="amazon",
            source="amazon_hunter",
            original_url="https://amazon.com.mx/dp/x",
            current_price=499.0,
            has_image=False,
            has_stock=True,
            brand="apple",
            category="audio_premium",
        )
        result = PriceErrorScorer().score(signal)
        assert result.is_publishable is False
        assert "no_image" in result.not_publishable_reasons

    def test_no_price_not_publishable(self):
        """Producto sin precio NUNCA publicable."""
        signal = PriceErrorSignal(
            product_title="Apple AirPods Pro",
            marketplace="amazon",
            source="amazon_hunter",
            original_url="https://amazon.com.mx/dp/x",
            current_price=None,
            has_image=True,
            has_stock=True,
        )
        result = PriceErrorScorer().score(signal)
        assert result.is_publishable is False
        assert "no_price" in result.not_publishable_reasons

    def test_used_product_requires_extreme_discount(self):
        """Un producto usado debe perder puntos vs el mismo nuevo."""
        scorer = PriceErrorScorer()
        new_signal = PriceErrorSignal(
            product_title="Apple iPhone 16 Pro Max 256GB",
            marketplace="liverpool",
            source="amazon_hunter",
            original_url="https://liverpool.com.mx/p/x",
            current_price=3899.0,
            has_image=True,
            has_stock=True,
            brand="apple",
            category="smartphone_flagship",
        )
        used_signal = PriceErrorSignal(
            product_title="Apple iPhone 16 Pro Max 256GB (usado)",
            marketplace="liverpool",
            source="amazon_hunter",
            original_url="https://liverpool.com.mx/p/x",
            current_price=3899.0,
            has_image=True,
            has_stock=True,
            brand="apple",
            category="smartphone_flagship",
            condition=Condition.USED.value,
        )
        new_score = scorer.score(new_signal).score
        used_score = scorer.score(used_signal).score
        assert used_score < new_score
        assert used_score <= new_score - 25

    def test_monthly_payment_not_misread_as_total_price(self):
        """Si se sospecha mensualidad, el resultado no es publicable."""
        signal = PriceErrorSignal(
            product_title="Laptop HP 14 pulgadas",
            marketplace="liverpool",
            source="amazon_hunter",
            original_url="https://liverpool.com.mx/p/x",
            current_price=899.0,  # parece error, pero es /mes
            has_image=True,
            has_stock=True,
            brand="hp",
            category="laptop",
            monthly_payment_suspected=True,
        )
        result = PriceErrorScorer().score(signal)
        assert result.is_publishable is False
        assert "monthly_payment" in result.not_publishable_reasons

    def test_variant_mismatch_reduces_confidence(self):
        """Variante seleccionada distinta del título => baja el score."""
        scorer = PriceErrorScorer()
        signal_clean = PriceErrorSignal(
            product_title="iPad Mini Apple WiFi 64GB",
            marketplace="sams_club",
            source="amazon_hunter",
            original_url="https://samsclub.com.mx/p/x",
            current_price=4999.0,
            has_image=True,
            has_stock=True,
            brand="apple",
            category="tablet",
        )
        signal_bad_variant = PriceErrorSignal(
            product_title="iPad Mini Apple WiFi 64GB",
            marketplace="sams_club",
            source="amazon_hunter",
            original_url="https://samsclub.com.mx/p/x",
            current_price=4999.0,
            has_image=True,
            has_stock=True,
            brand="apple",
            category="tablet",
            variant_mismatch=True,
        )
        clean = scorer.score(signal_clean).score
        bad = scorer.score(signal_bad_variant).score
        assert bad < clean
        assert bad <= clean - 30

    def test_accessory_price_not_confused_with_main_product(self):
        """Si el extractor marca accessory_price_suspected, score baja fuerte."""
        scorer = PriceErrorScorer()
        signal_main = PriceErrorSignal(
            product_title="Apple iPhone 16 Pro Max",
            marketplace="liverpool",
            source="amazon_hunter",
            original_url="https://liverpool.com.mx/p/x",
            current_price=399.0,
            has_image=True,
            has_stock=True,
            brand="apple",
            category="smartphone_flagship",
        )
        signal_accessory = PriceErrorSignal(
            product_title="Apple iPhone 16 Pro Max",
            marketplace="liverpool",
            source="amazon_hunter",
            original_url="https://liverpool.com.mx/p/x",
            current_price=399.0,
            has_image=True,
            has_stock=True,
            brand="apple",
            category="smartphone_flagship",
            accessory_price_suspected=True,
        )
        main_score = scorer.score(signal_main).score
        acc_score = scorer.score(signal_accessory).score
        assert acc_score < main_score
        assert main_score - acc_score >= 30


@pytest.mark.parametrize(
    "letter",
    ["a", "b", "c", "e", "g", "h", "i", "k", "l"],
)
def test_high_confidence_examples_classified_as_price_error_confirmed(
    letter, load_price_error_example
):
    """Los ejemplos A,B,C,E,G,H,I,K,L deben quedar como price_error_confirmed."""
    fixture = load_price_error_example(letter)
    signal = _signal_from_fixture(fixture)
    result = PriceErrorScorer().score(signal)

    assert result.classification == Classification.PRICE_ERROR_CONFIRMED.value, (
        f"Ejemplo {letter.upper()}: classification={result.classification} "
        f"score={result.score} reasons={result.reasons}"
    )
    assert result.score >= fixture["score_expected_min"]


@pytest.mark.parametrize("letter", ["d", "f", "j", "m"])
def test_medium_examples_at_least_possible_price_error(letter, load_price_error_example):
    """Ejemplos D, F, J, M deben quedar mínimo en possible_price_error."""
    fixture = load_price_error_example(letter)
    signal = _signal_from_fixture(fixture)
    result = PriceErrorScorer().score(signal)

    assert result.classification in (
        Classification.PRICE_ERROR_CONFIRMED.value,
        Classification.POSSIBLE_PRICE_ERROR.value,
    ), (
        f"Ejemplo {letter.upper()}: classification={result.classification} "
        f"score={result.score} reasons={result.reasons}"
    )
    assert result.score >= fixture["score_expected_min"]
