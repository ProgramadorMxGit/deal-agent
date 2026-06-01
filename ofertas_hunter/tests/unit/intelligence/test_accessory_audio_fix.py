"""Regresión: items de audio (Redmi Buds, JBL Flip, Sony WH-CH520) no deben
clasificarse como `price_error_confirmed` aunque tengan previous_price y
discount alto (típico false positive con `_fingerprint_smartphone` matcheando
"redmi" o "motorola" cuando es realmente un accesorio).

Caso histórico: outbox #123 publicó "XIAOMI Audífonos Redmi Buds 6 Play" como
ERROR DE PRECIO confirmado (score 85, previous=$1429, current=$199, disc=86%).
La página real NO tenía precio tachado: $1429 era el "list_price" (sugerido
fabricante), no un precio anterior real. Sin embargo el scorer sumó:
  - 30 pts: discount_visible_>=80
  - 35 pts: smartphone_below_500_extreme  ← FALSE POSITIVE (no es smartphone)
  - 20 pts: previous_vs_current_drop_70
Total 85 → price_error_confirmed.

Fix: añadir tokens audio (buds, audífonos, headphones, etc.) a
_ACCESSORY_TOKENS y a la guardia de _fingerprint_smartphone. Una vez detectado
como accessory genérico, GENERIC_ACCESSORY_PENALTY -45 + cap 55 lo degradan
a `suspicious_deal` con confianza media (no se publica como ERROR DE PRECIO).
"""

from __future__ import annotations

import pytest

from ofertas_hunter.intelligence.accessory_detector import (
    assess_title,
    is_generic_accessory,
)
from ofertas_hunter.intelligence.price_error_scorer import (
    PriceErrorScorer,
    _fingerprint_smartphone,
)
from ofertas_hunter.models import PriceErrorSignal


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "XIAOMI Audífonos Redmi Buds 6 Play Negro, Batería 36 Horas",
        "Sony Audífonos inalámbricos on-Ear WH-CH520",
        "JBL Flip 7 Bocina Portátil Bluetooth",
        "Galaxy Buds 2 Pro Audífonos inalámbricos",
        "Apple AirPods 4 Audífonos",  # accesorio Apple, no es iPhone
        "Bocina Bose SoundLink Mini",
        "Speaker portátil Anker SoundCore",
        "Smartwatch Xiaomi Mi Band 8",
        "Reloj inteligente Galaxy Watch 6",
    ],
)
def test_audio_and_smartwatch_detected_as_generic_accessory(title: str) -> None:
    """Los accesorios de audio y smartwatches deben clasificarse como
    accesorios genéricos para que el cap de score (55) entre en juego.
    """
    assert is_generic_accessory(title) is True, (
        f"'{title}' debería ser detectado como accesorio genérico"
    )


@pytest.mark.parametrize(
    "title",
    [
        "XIAOMI Audífonos Redmi Buds 6 Play Negro",
        "Sony WH-CH520 Audífonos inalámbricos",
        "JBL Flip 7 Bocina Bluetooth",
        "Smartwatch Galaxy Watch 6",
        "Pixel Watch 2 Reloj inteligente",
        "Motorola Smart TV 58 pulgadas",  # Motorola TV no es smartphone
    ],
)
def test_audio_and_smartwatch_not_smartphone(title: str) -> None:
    """`_fingerprint_smartphone` no debe matchear cuando el título
    describe audio/smartwatch/TV aunque mencione marcas como Redmi,
    Motorola, Galaxy, Pixel.
    """
    assert _fingerprint_smartphone(title) is False, (
        f"'{title}' NO debería matchear como smartphone"
    )


def test_real_smartphone_still_matches() -> None:
    """No queremos romper la detección de smartphones reales."""
    assert _fingerprint_smartphone("iPhone 15 Pro Max 256GB") is True
    assert _fingerprint_smartphone("Samsung Galaxy S24 Ultra 12GB 256GB") is True
    assert _fingerprint_smartphone("Motorola Moto G54 5G 128GB") is True
    assert _fingerprint_smartphone("Redmi Note 13 Pro 256GB Smartphone") is True


# ---------------------------------------------------------------------------
# Caso real: outbox #123 ya no debe ser price_error_confirmed
# ---------------------------------------------------------------------------


def test_redmi_buds_no_longer_price_error_confirmed() -> None:
    """Reproduce el payload exacto del item outbox #123 publicado por error.
    Con el fix, el score debe quedar capeado a 55 y la clasificación debe
    bajar de `price_error_confirmed` a algo distinto (típicamente
    `suspicious_deal` o `no_price_error`).
    """
    signal = PriceErrorSignal(
        product_title=(
            "XIAOMI Audífonos Redmi Buds 6 Play Negro, Batería de hasta 36 "
            "Horas, Driver de 10mm, 5 ecualizadores, reducción de Ruido AI "
            "para Llamadas"
        ),
        marketplace="amazon",
        source="amazon_hunter",
        original_url="https://www.amazon.com.mx/dp/B0DBHRWRKC",
        brand="Xiaomi",
        category=None,
        current_price=199.0,
        previous_price=1429.0,
        # Descuento visible alto + caída fuerte → eran las señales que
        # disparaban el FP. Aún con todas activadas, el cap debe contener.
        discount_percent=86.07,
        condition="new",
        has_stock=True,
        has_image=True,
    )

    scorer = PriceErrorScorer()
    result = scorer.score(signal)

    # No debe clasificarse como error de precio confirmado.
    assert result.classification != "price_error_confirmed", (
        f"Item de audio (Redmi Buds) NO debe ser price_error_confirmed; "
        f"score={result.score} reasons={result.reasons}"
    )

    # El score se cape a 55 (GENERIC_ACCESSORY_SCORE_CAP).
    assert result.score <= 55, (
        f"Score debe estar capeado a 55 para accesorios; obtuvo {result.score} "
        f"reasons={result.reasons}"
    )

    # Debe registrar la penalización de accesorio.
    assert any("generic_accessory" in r for r in result.reasons), (
        f"Esperaba ver razón generic_accessory en reasons={result.reasons}"
    )
