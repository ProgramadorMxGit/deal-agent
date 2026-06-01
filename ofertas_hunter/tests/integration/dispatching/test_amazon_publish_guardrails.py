"""Integración: el dispatcher nunca publica Amazon sin afiliado ni sin
precio anterior verificado, y usa affiliate_url cuando publica.

Cubre criterios de aceptación:
1. Amazon sin afiliado válido → NO publica (queda discarded).
2. Amazon con afiliado válido pero sin precio anterior verificado → NO publica.
3. Amazon con afiliado válido + descuento verificado → publica con affiliate_url.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ofertas_hunter.dispatching.cooldown import CooldownPolicy
from ofertas_hunter.dispatching.dispatcher import OutboxDispatcher, NullRevalidator
from ofertas_hunter.dispatching.outbox import InMemoryOutbox, OutboxConfig
from ofertas_hunter.models import OutboxItem, OutboxState, OutboxType
from ofertas_hunter.publishing.evolution_client import EvolutionClient
from ofertas_hunter.publishing.whatsapp_publisher import WhatsAppPublisher


T0 = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)


def _make(*, enabled: bool = True):
    outbox = InMemoryOutbox(
        OutboxConfig(
            revalidate_age_seconds=3600,
            cooldown=CooldownPolicy(cooldown_seconds=0),
        )
    )
    client = EvolutionClient(
        base_url="http://x", api_key="k", instance="i", dry_run=True
    )
    publisher = WhatsAppPublisher(
        client=client,
        target_group_id="120363@g.us",
        enabled=enabled,
        amazon_affiliate_required=True,
        amazon_min_discount_percent=50.0,
    )
    dispatcher = OutboxDispatcher(
        outbox=outbox,
        publisher=publisher,
        revalidator=NullRevalidator(),
        clock=lambda: T0,
    )
    return dispatcher, outbox


@pytest.mark.asyncio
async def test_dispatcher_does_not_publish_amazon_without_affiliate():
    dispatcher, outbox = _make()
    item = OutboxItem(
        offer_id=1,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "Producto Amazon",
            "current_price": 350,
            "previous_price": 800,
            "discount_percent": 56,
            "old_price_verified": True,
            "image_url": "http://x/i.jpg",
            "marketplace": "amazon",
            "url": "https://www.amazon.com.mx/dp/B0DL72GC7D",  # SIN afiliado
        },
    )
    outbox.enqueue(item)

    outcome = await dispatcher.tick()

    assert outcome is not None
    assert outcome.success is False
    assert outcome.discard_reason == "amazon_missing_affiliate"
    # El item quedó descartado, no pendiente, no publicado.
    assert not outbox.pending()


@pytest.mark.asyncio
async def test_dispatcher_does_not_publish_amazon_without_verified_old_price():
    dispatcher, outbox = _make()
    item = OutboxItem(
        offer_id=1,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "Sudadera $350",
            "current_price": 350,
            "previous_price": 1422.61,
            "discount_percent": 75,
            "old_price_verified": False,  # precio anterior NO verificado
            "image_url": "http://x/i.jpg",
            "marketplace": "amazon",
            "affiliate_url": "https://amzn.to/4abc",
            "url": "https://amzn.to/4abc",
        },
    )
    outbox.enqueue(item)

    outcome = await dispatcher.tick()

    assert outcome is not None
    assert outcome.success is False
    assert outcome.discard_reason == "amazon_no_verified_old_price"
    assert not outbox.pending()


@pytest.mark.asyncio
async def test_dispatcher_publishes_amazon_with_affiliate_using_affiliate_url():
    dispatcher, outbox = _make()
    item = OutboxItem(
        offer_id=1,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "Producto Real Amazon",
            "current_price": 350,
            "previous_price": 800,  # real 56%
            "discount_percent": 56,
            "old_price_verified": True,
            "image_url": "http://x/i.jpg",
            "marketplace": "amazon",
            "affiliate_url": "https://amzn.to/4abc",
            "url": "https://www.amazon.com.mx/dp/B0DL72GC7D",  # directa
        },
    )
    outbox.enqueue(item)

    outcome = await dispatcher.tick()

    assert outcome is not None
    assert outcome.success is True
    assert outcome.formatted is not None
    # Publica usando el affiliate_url, NUNCA la URL directa.
    assert "https://amzn.to/4abc" in outcome.formatted.text
    assert "amazon.com.mx/dp/B0DL72GC7D" not in outcome.formatted.text
