"""Tests del WhatsAppPublisher (formatter + evolution_client integrados)."""

from __future__ import annotations

import pytest

from ofertas_hunter.models import OutboxItem, OutboxType
from ofertas_hunter.publishing.evolution_client import EvolutionClient
from ofertas_hunter.publishing.whatsapp_publisher import WhatsAppPublisher


def _normal_payload() -> dict:
    return {
        "title": "JBL Tune 510BT - Auriculares Bluetooth",
        "current_price": 388,
        "previous_price": 899,
        "discount_percent": 57,
        "url": "https://amzn.to/4e3yTjG",
        "image_url": "https://m.media-amazon.com/images/I/abc.jpg",
    }


def _price_error_payload() -> dict:
    return {
        "title": "Apple iPhone 16 Pro Max 256GB",
        "current_price": 3899,
        "url": "https://liverpool.com.mx/p/x",
        "image_url": "https://liverpool.com.mx/img.jpg",
        "confidence_label": "very high",
        "marketplace": "liverpool",
    }


@pytest.fixture
def dry_run_client() -> EvolutionClient:
    return EvolutionClient(
        base_url="http://x:8080", api_key="k", instance="i", dry_run=True
    )


@pytest.mark.asyncio
async def test_publishes_normal_offer_in_dry_run(dry_run_client):
    publisher = WhatsAppPublisher(
        client=dry_run_client, target_group_id="120363@g.us", enabled=True
    )
    item = OutboxItem(
        offer_id=1,
        type=OutboxType.NORMAL.value,
        message_payload=_normal_payload(),
    )
    result = await publisher.publish(item)
    assert result.success is True
    assert result.dry_run is True
    assert "AHORA: $388" in result.formatted.text
    assert "🔥 *57% de descuento*" in result.formatted.text


@pytest.mark.asyncio
async def test_publishes_price_error_in_dry_run(dry_run_client):
    publisher = WhatsAppPublisher(
        client=dry_run_client, target_group_id="120363@g.us", enabled=True
    )
    item = OutboxItem(
        offer_id=1,
        type=OutboxType.PRICE_ERROR.value,
        message_payload=_price_error_payload(),
    )
    result = await publisher.publish(item)
    assert result.success is True
    assert "🚨 ERROR DE PRECIO 🚨" in result.formatted.text


@pytest.mark.asyncio
async def test_publisher_disabled_returns_skipped(dry_run_client):
    publisher = WhatsAppPublisher(
        client=dry_run_client, target_group_id="120363@g.us", enabled=False
    )
    item = OutboxItem(
        offer_id=1,
        type=OutboxType.NORMAL.value,
        message_payload=_normal_payload(),
    )
    result = await publisher.publish(item)
    assert result.skipped is True
    assert result.skip_reason == "publishing_disabled"
    assert result.success is False


@pytest.mark.asyncio
async def test_publish_without_image_fails(dry_run_client):
    publisher = WhatsAppPublisher(
        client=dry_run_client, target_group_id="120363@g.us", enabled=True
    )
    payload = _normal_payload()
    payload["image_url"] = ""
    item = OutboxItem(offer_id=1, type=OutboxType.NORMAL.value, message_payload=payload)
    result = await publisher.publish(item)
    assert result.success is False
    assert "image_url" in (result.error or "")


@pytest.mark.asyncio
async def test_publish_without_price_fails(dry_run_client):
    publisher = WhatsAppPublisher(
        client=dry_run_client, target_group_id="120363@g.us", enabled=True
    )
    payload = _normal_payload()
    payload["current_price"] = None
    item = OutboxItem(offer_id=1, type=OutboxType.NORMAL.value, message_payload=payload)
    result = await publisher.publish(item)
    assert result.success is False
    assert "current_price" in (result.error or "")


@pytest.mark.asyncio
async def test_publish_without_target_group_fails():
    client = EvolutionClient(
        base_url="http://x", api_key="k", instance="i", dry_run=True
    )
    publisher = WhatsAppPublisher(client=client, target_group_id="", enabled=True)
    item = OutboxItem(
        offer_id=1, type=OutboxType.NORMAL.value, message_payload=_normal_payload()
    )
    result = await publisher.publish(item)
    assert result.success is False
    assert result.error == "target_group_id_unset"
