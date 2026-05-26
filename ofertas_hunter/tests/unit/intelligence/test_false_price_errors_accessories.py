"""Tests de regresión para los falsos positivos de mayo 2026.

Tres cargadores 20W "compatible con iPhone" en Mercado Libre fueron
publicados como **ERROR DE PRECIO** con confianza `medium`. Ninguno de
ellos es un error de precio: son accesorios genéricos cuyo precio normal
ronda los $50–$300 MXN.

Este archivo valida que:

1. El `PriceErrorScorer` ya no clasifica esos títulos como
   `price_error_confirmed`.
2. La razón `generic_accessory_not_price_error` aparece en `reasons`.
3. La razón `compatible_with_premium_brand_not_premium_product` aparece
   en `reasons` (los tres dicen "compatible con iPhone").
4. El score nunca alcanza el cap superior `60` que dispararía
   `possible_price_error`.
5. Si el item llegara al outbox como `price_error` con confianza media,
   el publisher lo bloquea.
6. Si tiene descuento real >= 50%, puede degradarse a oferta normal.
7. Si no, se descarta con razón explícita.
8. El detector tampoco confunde "compatible con iPhone" con un producto
   Apple real.

Tests adicionales (REGLA 3, 5, 6, 7):

- `test_compatible_with_iphone_does_not_count_as_apple_product`
- `test_compatible_with_samsung_does_not_count_as_samsung_flagship`
- `test_generic_accessory_caps_confidence_to_medium`
- `test_medium_confidence_price_error_not_publishable`
- `test_dispatcher_blocks_medium_price_error`
- `test_ml_generic_accessory_requires_real_discount_for_normal_offer`
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ofertas_hunter.dispatching.cooldown import CooldownPolicy
from ofertas_hunter.dispatching.dispatcher import OutboxDispatcher
from ofertas_hunter.dispatching.outbox import InMemoryOutbox, OutboxConfig
from ofertas_hunter.intelligence.accessory_detector import (
    assess_title,
    is_generic_accessory,
    is_real_premium_product,
    mentions_compatible_with_premium,
)
from ofertas_hunter.intelligence.price_error_scorer import PriceErrorScorer
from ofertas_hunter.models import (
    Classification,
    OutboxItem,
    OutboxState,
    OutboxType,
    PriceErrorSignal,
)
from ofertas_hunter.publishing.evolution_client import EvolutionClient
from ofertas_hunter.publishing.formatter import FormatterError, format_price_error
from ofertas_hunter.publishing.whatsapp_publisher import WhatsAppPublisher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "fixtures"
    / "price_errors"
)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _signal(fixture: dict) -> PriceErrorSignal:
    return PriceErrorSignal(
        product_title=fixture["product_title"],
        marketplace=fixture["marketplace"],
        source=fixture["source"],
        original_url=fixture["original_url"],
        source_channel=fixture.get("source_channel"),
        original_text=fixture.get("original_text"),
        resolved_url=fixture.get("resolved_url"),
        current_price=fixture.get("current_price"),
        previous_price=fixture.get("previous_price"),
        historical_median_price=fixture.get("historical_median_price"),
        discount_percent=fixture.get("discount_percent"),
        brand=fixture.get("brand"),
        category=fixture.get("category"),
        condition=fixture.get("condition", "new"),
        has_stock=fixture.get("has_stock", True),
        has_image=fixture.get("has_image", True),
        urgency_terms=list(fixture.get("urgency_terms") or []),
    )


# ---------------------------------------------------------------------------
# Los 3 falsos positivos críticos
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_name, expected_label",
    [
        ("false_positive_charger_76", "false_positive_generic_charger_76"),
        ("false_positive_charger_93", "false_positive_generic_charger_93"),
        (
            "false_positive_charger_cable_160",
            "false_positive_generic_charger_cable_160",
        ),
    ],
)
class TestKnownFalsePositiveChargers:
    """Cubre `test_false_price_error_generic_charger_{76,93,160}`."""

    def test_no_classification_price_error_confirmed(
        self, fixture_name, expected_label
    ):
        result = PriceErrorScorer().score(_signal(_load(fixture_name)))
        assert result.classification != Classification.PRICE_ERROR_CONFIRMED.value, (
            f"{expected_label}: classification={result.classification} "
            f"score={result.score} reasons={result.reasons}"
        )

    def test_no_outbox_type_price_error(self, fixture_name, expected_label):
        """Validamos que el `confidence_label` resultante NO permita que el
        formatter publique como ERROR DE PRECIO. La degradación efectiva la
        prueba `test_dispatcher_blocks_medium_price_error`.
        """
        result = PriceErrorScorer().score(_signal(_load(fixture_name)))
        # Regla 7: ningún PE con confianza < high es publicable.
        assert result.confidence_label not in ("high", "very high"), (
            f"{expected_label}: confidence={result.confidence_label} debería ser <=medium"
        )
        # Mejor evidencia: el formatter rechaza explícitamente medium.
        with pytest.raises(FormatterError):
            format_price_error(
                title=_load(fixture_name)["product_title"],
                current_price=float(_load(fixture_name)["current_price"]),
                confidence_label=result.confidence_label,
                marketplace=_load(fixture_name)["marketplace"],
                url=_load(fixture_name)["original_url"],
                image_url="https://example.com/img.jpg",
            )

    def test_no_bypass_cooldown(self, fixture_name, expected_label):
        """Score debe estar por debajo del threshold `>=60` que asignaría
        `possible_price_error` y por tanto bypass cooldown.
        """
        result = PriceErrorScorer().score(_signal(_load(fixture_name)))
        assert result.classification not in (
            Classification.PRICE_ERROR_CONFIRMED.value,
            Classification.POSSIBLE_PRICE_ERROR.value,
        ), (
            f"{expected_label}: classification={result.classification} aún permite bypass cooldown"
        )

    def test_reason_includes_generic_accessory(
        self, fixture_name, expected_label
    ):
        result = PriceErrorScorer().score(_signal(_load(fixture_name)))
        joined = " | ".join(result.reasons)
        assert "generic_accessory_not_price_error" in joined, (
            f"{expected_label}: reasons no incluyen "
            f"generic_accessory_not_price_error → {joined}"
        )

    def test_reason_includes_compatible_with_premium(
        self, fixture_name, expected_label
    ):
        result = PriceErrorScorer().score(_signal(_load(fixture_name)))
        joined = " | ".join(result.reasons)
        assert "compatible_with_premium_brand_not_premium_product" in joined, (
            f"{expected_label}: reasons no incluyen el penalty de compat "
            f"→ {joined}"
        )

    def test_can_be_normal_offer_when_real_discount(
        self, fixture_name, expected_label
    ):
        """Si añadimos `discount_percent>=50`, el publisher debe degradar el
        item a oferta normal (no descartarlo).
        """
        fixture = _load(fixture_name)
        # Construimos un item PE como si el agente lo hubiera enviado por
        # error a la outbox con confianza medium.
        payload = {
            "title": fixture["product_title"],
            "current_price": fixture["current_price"],
            "previous_price": float(fixture["current_price"]) * 2.5,
            "discount_percent": 60.0,
            "url": fixture["original_url"],
            "image_url": "https://example.com/img.jpg",
            "confidence_label": "medium",
            "marketplace": fixture["marketplace"],
            "affiliate_url": fixture["original_url"],  # ML gate
        }
        item = OutboxItem(
            offer_id=1,
            type=OutboxType.PRICE_ERROR.value,
            message_payload=payload,
        )
        # Probamos sólo la decisión interna del publisher (no llamamos
        # a Evolution para no requerir transport real).
        client = EvolutionClient(
            base_url="http://x", api_key="k", instance="i", dry_run=True
        )
        publisher = WhatsAppPublisher(
            client=client, target_group_id="g@g.us", enabled=True
        )
        guard_outcome = publisher._medium_pe_guardrail(item)  # noqa: SLF001
        assert guard_outcome is None, (
            "Con discount >= 50% el guardrail debe permitir continuar para "
            "degradar a normal."
        )
        degraded = publisher._maybe_degrade_item(item)  # noqa: SLF001
        assert degraded.type == OutboxType.NORMAL.value
        assert degraded.message_payload["degraded_from"] == OutboxType.PRICE_ERROR.value
        assert degraded.message_payload["discount_percent"] == 60.0

    def test_discarded_when_no_real_discount(self, fixture_name, expected_label):
        """Sin descuento real >=50%, el publisher devuelve `discard_reason`."""
        fixture = _load(fixture_name)
        payload = {
            "title": fixture["product_title"],
            "current_price": fixture["current_price"],
            "url": fixture["original_url"],
            "image_url": "https://example.com/img.jpg",
            "confidence_label": "medium",
            "marketplace": fixture["marketplace"],
            "affiliate_url": fixture["original_url"],
        }
        item = OutboxItem(
            offer_id=1,
            type=OutboxType.PRICE_ERROR.value,
            message_payload=payload,
        )
        client = EvolutionClient(
            base_url="http://x", api_key="k", instance="i", dry_run=True
        )
        publisher = WhatsAppPublisher(
            client=client, target_group_id="g@g.us", enabled=True
        )
        outcome = publisher._medium_pe_guardrail(item)  # noqa: SLF001
        assert outcome is not None
        assert outcome.success is False
        assert outcome.discard_reason == "discarded_false_price_error_medium_confidence"


# ---------------------------------------------------------------------------
# REGLA 3 — "compatible con / para <marca>" no convierte en producto premium
# ---------------------------------------------------------------------------


def test_compatible_with_iphone_does_not_count_as_apple_product():
    """`compatible con iPhone` y `para iPhone` no son productos Apple."""
    titles = [
        "Cargador 20W Compatible con iPhone 14 / 13 / 12",
        "Funda silicón para iPhone 16 Pro",
        "Cable USB C a Lightning compatible con iPhone",
        "Cargador rápido 20w + 2 metros cable tipo c para iphone",
    ]
    for title in titles:
        assessment = assess_title(title)
        assert assessment.mentions_compatible_with_premium is True, title
        assert assessment.is_real_premium_product is False, title
        assert assessment.is_generic_accessory is True, title

    # Por contraste, "Apple iPhone 16 Pro Max 256GB" sí es producto premium.
    real_iphone = assess_title("Apple iPhone 16 Pro Max 256GB Titanio Natural")
    assert real_iphone.is_real_premium_product is True
    assert real_iphone.is_generic_accessory is False
    assert real_iphone.mentions_compatible_with_premium is False


def test_compatible_with_samsung_does_not_count_as_samsung_flagship():
    """`compatible con Samsung` y `para Galaxy` no son flagships."""
    titles = [
        "Cargador 25W compatible con Samsung Galaxy S24",
        "Funda compatible con Samsung Galaxy S23 Ultra",
        "Cable type-c para Samsung Galaxy Note 20",
    ]
    for title in titles:
        assessment = assess_title(title)
        assert assessment.mentions_compatible_with_premium is True, title
        assert assessment.is_real_premium_product is False, title
        assert assessment.is_generic_accessory is True, title

    # Galaxy real = sí flagship.
    real_galaxy = assess_title("Samsung Galaxy S24 Ultra 512GB")
    assert real_galaxy.is_real_premium_product is True
    assert real_galaxy.is_generic_accessory is False


# ---------------------------------------------------------------------------
# REGLA 5 — accesorio genérico cap confianza a medium
# ---------------------------------------------------------------------------


def test_generic_accessory_caps_confidence_to_medium():
    """Aún con bonus de marca premium hardcodeado, un accesorio se cap a medium."""
    signal = PriceErrorSignal(
        product_title=(
            "Cargador TIPO C GaN Turbo 20W Compatible con iPhone 14 13 12"
        ),
        marketplace="mercadolibre",
        source="mercadolibre_hunter",
        original_url="https://articulo.mercadolibre.com.mx/MLM-x",
        current_price=76.0,
        brand="apple",
        category=None,
        has_image=True,
        has_stock=True,
    )
    result = PriceErrorScorer().score(signal)
    assert result.confidence_label not in ("very high", "high")
    assert result.classification != Classification.PRICE_ERROR_CONFIRMED.value


# ---------------------------------------------------------------------------
# REGLA 1 / 7 — medium no es publicable como ERROR DE PRECIO
# ---------------------------------------------------------------------------


def test_medium_confidence_price_error_not_publishable():
    """`format_price_error` rechaza confidence='medium'."""
    with pytest.raises(FormatterError):
        format_price_error(
            title="Cargador 20W",
            current_price=76.0,
            confidence_label="medium",
            marketplace="mercadolibre",
            url="https://articulo.mercadolibre.com.mx/MLM-x",
            image_url="https://example.com/img.jpg",
        )


# ---------------------------------------------------------------------------
# REGLA 6 — dispatcher bloquea medium-PE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_blocks_medium_price_error():
    """Un outbox item type=price_error con confidence_label=medium NO se publica."""
    outbox = InMemoryOutbox(
        OutboxConfig(cooldown=CooldownPolicy(cooldown_seconds=300))
    )
    client = EvolutionClient(
        base_url="http://x", api_key="k", instance="i", dry_run=True
    )
    publisher = WhatsAppPublisher(
        client=client,
        target_group_id="g@g.us",
        enabled=True,
        mercadolibre_affiliate_required=False,
    )
    dispatcher = OutboxDispatcher(outbox=outbox, publisher=publisher)

    item = outbox.enqueue(
        OutboxItem(
            offer_id=1,
            type=OutboxType.PRICE_ERROR.value,
            message_payload={
                "title": "Cargador 20w Compatible con iPhone",
                "current_price": 93.98,
                "url": "https://articulo.mercadolibre.com.mx/MLM-FP2",
                "image_url": "https://example.com/img.jpg",
                "confidence_label": "medium",
                "marketplace": "mercadolibre",
            },
        )
    )

    outcome = await dispatcher.tick()
    assert outcome is not None
    assert outcome.success is False
    assert outcome.discard_reason == "discarded_false_price_error_medium_confidence"

    persisted = next(i for i in outbox if i.id == item.id)
    assert persisted.state == OutboxState.DISCARDED.value
    assert (
        persisted.message_payload.get("discarded_reason")
        == "discarded_false_price_error_medium_confidence"
    )


# ---------------------------------------------------------------------------
# REGLA 4 — accesorio genérico requiere descuento real para ser oferta normal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ml_generic_accessory_requires_real_discount_for_normal_offer():
    """Sin descuento real, un accesorio ML medium-PE se descarta. Con
    descuento >=50% el publisher lo degrada y publica como oferta normal.
    """
    outbox = InMemoryOutbox()
    client = EvolutionClient(
        base_url="http://x", api_key="k", instance="i", dry_run=True
    )
    publisher = WhatsAppPublisher(
        client=client,
        target_group_id="g@g.us",
        enabled=True,
        mercadolibre_affiliate_required=False,
    )
    dispatcher = OutboxDispatcher(outbox=outbox, publisher=publisher)

    # Caso A: sin descuento → descarte
    item_a = outbox.enqueue(
        OutboxItem(
            offer_id=10,
            type=OutboxType.PRICE_ERROR.value,
            message_payload={
                "title": "Cargador 20w Compatible con iPhone",
                "current_price": 93.98,
                "url": "https://articulo.mercadolibre.com.mx/MLM-A",
                "image_url": "https://example.com/img.jpg",
                "confidence_label": "medium",
                "marketplace": "mercadolibre",
            },
        )
    )
    out_a = await dispatcher.tick()
    assert out_a is not None
    assert out_a.success is False
    assert out_a.discard_reason
    persisted_a = next(i for i in outbox if i.id == item_a.id)
    assert persisted_a.state == OutboxState.DISCARDED.value

    # Caso B: con descuento real >=50 → degrade a normal y publica.
    item_b = outbox.enqueue(
        OutboxItem(
            offer_id=11,
            type=OutboxType.PRICE_ERROR.value,
            message_payload={
                "title": "Cargador 20w Compatible con iPhone",
                "current_price": 93.98,
                "previous_price": 250.0,
                "discount_percent": 62.0,
                "url": "https://articulo.mercadolibre.com.mx/MLM-B",
                "image_url": "https://example.com/img.jpg",
                "confidence_label": "medium",
                "marketplace": "mercadolibre",
            },
        )
    )
    out_b = await dispatcher.tick()
    assert out_b is not None
    assert out_b.success is True
    assert out_b.degraded_outbox_type == OutboxType.NORMAL.value
    assert out_b.formatted is not None and out_b.formatted.type == "normal"
    persisted_b = next(i for i in outbox if i.id == item_b.id)
    assert persisted_b.state == OutboxState.SENT.value
    # El histórico ya muestra type=normal.
    assert persisted_b.type == OutboxType.NORMAL.value


# ---------------------------------------------------------------------------
# Negativos: la corrección NO debe romper los errores premium reales
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "letter, expected_min_confidence",
    [
        ("i", "very high"),  # iPhone 16 Pro Max
        ("k", "very high"),  # Galaxy S24
        ("b", "very high"),  # Laptop Asus + combo
        ("h", "high"),  # AirPods Pro $599
    ],
)
def test_real_premium_price_errors_still_classified(
    letter, expected_min_confidence, load_price_error_example
):
    """iPhone 16 Pro Max, Galaxy S24, Laptop, AirPods Pro siguen siendo
    `price_error_confirmed` con high/very high.
    """
    fixture = load_price_error_example(letter)
    signal = _signal(fixture)
    result = PriceErrorScorer().score(signal)

    assert result.classification == Classification.PRICE_ERROR_CONFIRMED.value, (
        f"{fixture['product_title']!r}: classification={result.classification}"
    )
    if expected_min_confidence == "very high":
        assert result.confidence_label == "very high"
    else:
        assert result.confidence_label in ("high", "very high")
