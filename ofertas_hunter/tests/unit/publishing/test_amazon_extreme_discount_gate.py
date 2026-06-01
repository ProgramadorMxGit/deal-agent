"""Guardrail de defensa en profundidad para precios extremos / unit-price Amazon.

Aunque el extractor falle y guarde un precio por unidad como `current_price`,
el publisher DEBE bloquear:
- Amazon con current_price < 10 y old_price > 50 (precio sospechoso).
- Amazon con discount >= 90% sin `extreme_discount_verified=true`.
- Amazon cuyo `current_price_raw_text` contenga `/ unidad`, `por unidad`, etc.
- Amazon cuyo current_price sea < 1% del old_price.
"""

from __future__ import annotations

import pytest

from ofertas_hunter.models import OutboxItem, OutboxType
from ofertas_hunter.publishing.evolution_client import EvolutionClient
from ofertas_hunter.publishing.whatsapp_publisher import WhatsAppPublisher


@pytest.fixture
def dry_run_client() -> EvolutionClient:
    return EvolutionClient(
        base_url="http://x:8080", api_key="k", instance="i", dry_run=True
    )


def _pub(client) -> WhatsAppPublisher:
    return WhatsAppPublisher(
        client=client,
        target_group_id="120363@g.us",
        enabled=True,
        amazon_affiliate_required=True,
        amazon_min_discount_percent=50.0,
    )


def _gloves(**over):
    base = {
        "title": "SELECTSHOP Pack De 100 Guantes De Nitrilo",
        "image_url": "http://x/i.jpg",
        "current_price": 0.69,
        "previous_price": 119.0,
        "discount_percent": 99,
        "old_price_verified": True,
        "discount_percent_verified": True,
        "marketplace": "amazon",
        "affiliate_url": "https://amzn.to/4u6nep8",
        "url": "https://amzn.to/4u6nep8",
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_blocks_extreme_discount_without_extreme_verified(dry_run_client):
    """99% sin extreme_discount_verified → bloqueado."""
    item = OutboxItem(offer_id=1, type=OutboxType.NORMAL.value, message_payload=_gloves())
    out = await _pub(dry_run_client).publish(item)
    assert out.success is False
    assert out.discard_reason == "amazon_extreme_discount_unverified"


@pytest.mark.asyncio
async def test_blocks_suspicious_low_current_price(dry_run_client):
    """current_price < 10 con old_price > 50 → sospechoso (precio por unidad)."""
    # discount 85 (no extremo) pero precio absoluto sospechoso.
    item = OutboxItem(
        offer_id=1, type=OutboxType.NORMAL.value,
        message_payload=_gloves(current_price=7.0, previous_price=119.0, discount_percent=94),
    )
    out = await _pub(dry_run_client).publish(item)
    assert out.success is False
    # 94% es extremo → cae primero en extreme; ambos son válidos como bloqueo.
    assert out.discard_reason in (
        "amazon_extreme_discount_unverified",
        "amazon_current_price_suspicious",
    )


@pytest.mark.asyncio
async def test_blocks_unit_price_raw_text(dry_run_client):
    """current_price_raw_text con '/ unidad' → bloqueado aunque el % sea normal."""
    item = OutboxItem(
        offer_id=1, type=OutboxType.NORMAL.value,
        message_payload=_gloves(
            current_price=60.0, previous_price=119.0, discount_percent=50,
            current_price_raw_text="$0.69 / unidad",
        ),
    )
    out = await _pub(dry_run_client).publish(item)
    assert out.success is False
    assert out.discard_reason == "amazon_unit_price_as_current_price"


@pytest.mark.asyncio
async def test_blocks_current_below_one_percent_of_old(dry_run_client):
    """current_price < 1% del old_price → bloqueado."""
    item = OutboxItem(
        offer_id=1, type=OutboxType.NORMAL.value,
        message_payload=_gloves(current_price=0.6, previous_price=332.0, discount_percent=80),
    )
    out = await _pub(dry_run_client).publish(item)
    assert out.success is False
    assert out.discard_reason in (
        "amazon_current_price_suspicious",
        "amazon_extreme_discount_unverified",
    )


@pytest.mark.asyncio
async def test_extreme_discount_passes_when_verified(dry_run_client):
    """99% CON extreme_discount_verified=true y precio total razonable → publica.

    (Un error de precio real validado: precio actual >= 10 y no unit price.)
    """
    item = OutboxItem(
        offer_id=1, type=OutboxType.NORMAL.value,
        message_payload=_gloves(
            current_price=30.0, previous_price=3000.0, discount_percent=99,
            extreme_discount_verified=True,
            current_price_raw_text="$30.00",
        ),
    )
    out = await _pub(dry_run_client).publish(item)
    assert out.success is True


@pytest.mark.asyncio
async def test_normal_amazon_still_publishes(dry_run_client):
    """Amazon normal (56%, precio total correcto) sigue publicando."""
    item = OutboxItem(
        offer_id=1, type=OutboxType.NORMAL.value,
        message_payload=_gloves(
            title="CeraVe Crema", current_price=155.0, previous_price=349.0,
            discount_percent=56, current_price_raw_text="$155.00",
        ),
    )
    out = await _pub(dry_run_client).publish(item)
    assert out.success is True
    assert "amzn.to" in out.formatted.text
