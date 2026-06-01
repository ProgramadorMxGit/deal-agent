"""Tests del guardrail duro de Amazon en el WhatsAppPublisher.

Reglas:
- Amazon SIN affiliate_url válido (amzn.to/ o tag=) → no se publica.
- Amazon NORMAL (oferta de descuento) sin `old_price_verified` → no se publica.
- Amazon NORMAL con descuento que no cuadra con (prev-cur)/prev → no se publica.
- Amazon NORMAL con affiliate válido + old_price_verified + descuento real → publica
  usando affiliate_url.
- El caso real de la sudadera ($350, previous 1422.61, no verificado) se descarta.
"""

from __future__ import annotations

import pytest

from ofertas_hunter.models import OutboxItem, OutboxType
from ofertas_hunter.publishing.evolution_client import EvolutionClient
from ofertas_hunter.publishing.whatsapp_publisher import (
    WhatsAppPublisher,
    _is_valid_affiliate_url,
)


@pytest.fixture
def dry_run_client() -> EvolutionClient:
    return EvolutionClient(
        base_url="http://x:8080", api_key="k", instance="i", dry_run=True
    )


def _amazon_publisher(client) -> WhatsAppPublisher:
    return WhatsAppPublisher(
        client=client,
        target_group_id="120363@g.us",
        enabled=True,
        amazon_affiliate_required=True,
        amazon_min_discount_percent=50.0,
    )


# ---------------------------------------------------------------------------
# _is_valid_affiliate_url
# ---------------------------------------------------------------------------


def test_amzn_short_link_is_valid():
    assert _is_valid_affiliate_url("https://amzn.to/4abc") is True


def test_tag_param_is_valid():
    assert (
        _is_valid_affiliate_url("https://www.amazon.com.mx/dp/B0X?tag=ofertones03-20")
        is True
    )


def test_plain_dp_url_is_invalid():
    assert _is_valid_affiliate_url("https://www.amazon.com.mx/dp/B0DXMMNHYV") is False


def test_empty_is_invalid():
    assert _is_valid_affiliate_url("") is False
    assert _is_valid_affiliate_url(None) is False


# ---------------------------------------------------------------------------
# Gate: affiliate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amazon_blocks_without_affiliate(dry_run_client):
    publisher = _amazon_publisher(dry_run_client)
    item = OutboxItem(
        offer_id=1,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "Producto",
            "image_url": "http://x/i.jpg",
            "current_price": 350,
            "previous_price": 800,
            "discount_percent": 56,
            "old_price_verified": True,
            "marketplace": "amazon",
            "url": "https://www.amazon.com.mx/dp/B0DL72GC7D",  # sin afiliado
        },
    )
    out = await publisher.publish(item)
    assert out.success is False
    assert out.discard_reason == "amazon_missing_affiliate"


@pytest.mark.asyncio
async def test_amazon_blocks_invalid_affiliate_field(dry_run_client):
    publisher = _amazon_publisher(dry_run_client)
    item = OutboxItem(
        offer_id=1,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "Producto",
            "image_url": "http://x/i.jpg",
            "current_price": 350,
            "previous_price": 800,
            "discount_percent": 56,
            "old_price_verified": True,
            "marketplace": "amazon",
            # affiliate_url presente pero sin amzn.to ni tag= → inválido
            "affiliate_url": "https://www.amazon.com.mx/dp/B0DL72GC7D",
            "url": "https://www.amazon.com.mx/dp/B0DL72GC7D",
        },
    )
    out = await publisher.publish(item)
    assert out.success is False
    assert out.discard_reason == "amazon_missing_affiliate"


# ---------------------------------------------------------------------------
# Gate: precio anterior verificado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amazon_blocks_unverified_old_price(dry_run_client):
    """Caso real sudadera $350: previous inventado, no verificado."""
    publisher = _amazon_publisher(dry_run_client)
    item = OutboxItem(
        offer_id=1,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "Sudadera con Capucha",
            "image_url": "http://x/i.jpg",
            "current_price": 350,
            "previous_price": 1422.61,
            "discount_percent": 75,
            "old_price_verified": False,
            "marketplace": "amazon",
            "affiliate_url": "https://amzn.to/4abc",
            "url": "https://amzn.to/4abc",
        },
    )
    out = await publisher.publish(item)
    assert out.success is False
    assert out.discard_reason == "amazon_no_verified_old_price"


@pytest.mark.asyncio
async def test_amazon_blocks_old_price_not_greater_than_current(dry_run_client):
    publisher = _amazon_publisher(dry_run_client)
    item = OutboxItem(
        offer_id=1,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "Producto",
            "image_url": "http://x/i.jpg",
            "current_price": 350,
            "previous_price": 350,  # igual → no hay descuento real
            "discount_percent": 0,
            "old_price_verified": True,
            "marketplace": "amazon",
            "affiliate_url": "https://amzn.to/4abc",
            "url": "https://amzn.to/4abc",
        },
    )
    out = await publisher.publish(item)
    assert out.success is False
    assert out.discard_reason == "amazon_no_verified_old_price"


@pytest.mark.asyncio
async def test_amazon_blocks_discount_mismatch(dry_run_client):
    """discount_percent declarado no cuadra con (prev-cur)/prev (>2 pts)."""
    publisher = _amazon_publisher(dry_run_client)
    item = OutboxItem(
        offer_id=1,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "Producto",
            "image_url": "http://x/i.jpg",
            "current_price": 350,
            "previous_price": 700,  # real = 50%
            "discount_percent": 75,  # declarado = 75% → mismatch
            "old_price_verified": True,
            "marketplace": "amazon",
            "affiliate_url": "https://amzn.to/4abc",
            "url": "https://amzn.to/4abc",
        },
    )
    out = await publisher.publish(item)
    assert out.success is False
    assert out.discard_reason == "amazon_discount_mismatch"


@pytest.mark.asyncio
async def test_amazon_blocks_below_min_discount(dry_run_client):
    publisher = _amazon_publisher(dry_run_client)
    item = OutboxItem(
        offer_id=1,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "Producto",
            "image_url": "http://x/i.jpg",
            "current_price": 900,
            "previous_price": 1000,  # real = 10%
            "discount_percent": 10,
            "old_price_verified": True,
            "marketplace": "amazon",
            "affiliate_url": "https://amzn.to/4abc",
            "url": "https://amzn.to/4abc",
        },
    )
    out = await publisher.publish(item)
    assert out.success is False
    assert out.discard_reason == "amazon_below_min_discount"


# ---------------------------------------------------------------------------
# Camino feliz
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amazon_publishes_valid_offer_using_affiliate_url(dry_run_client):
    publisher = _amazon_publisher(dry_run_client)
    item = OutboxItem(
        offer_id=1,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "Producto Real",
            "image_url": "http://x/i.jpg",
            "current_price": 350,
            "previous_price": 800,  # real = 56%
            "discount_percent": 56,
            "old_price_verified": True,
            "marketplace": "amazon",
            "affiliate_url": "https://amzn.to/4abc",
            "url": "https://amzn.to/4abc",
        },
    )
    out = await publisher.publish(item)
    assert out.success is True
    assert out.formatted is not None
    # El link publicado debe ser el de afiliado.
    assert "https://amzn.to/4abc" in out.formatted.text


@pytest.mark.asyncio
async def test_amazon_price_error_does_not_require_old_price(dry_run_client):
    """Un price_error de Amazon (confianza high) no exige old_price_verified,
    pero SÍ exige afiliado válido."""
    publisher = _amazon_publisher(dry_run_client)
    item = OutboxItem(
        offer_id=1,
        type=OutboxType.PRICE_ERROR.value,
        message_payload={
            "title": "Producto PE",
            "image_url": "http://x/i.jpg",
            "current_price": 99,
            "confidence_label": "high",
            "marketplace": "amazon",
            "affiliate_url": "https://amzn.to/4abc",
            "url": "https://amzn.to/4abc",
        },
    )
    out = await publisher.publish(item)
    assert out.success is True


@pytest.mark.asyncio
async def test_non_amazon_unaffected_by_amazon_gate(dry_run_client):
    """Un item ML no debe verse afectado por el gate de Amazon."""
    publisher = _amazon_publisher(dry_run_client)
    item = OutboxItem(
        offer_id=1,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "Producto ML",
            "image_url": "http://x/i.jpg",
            "current_price": 350,
            "previous_price": 800,
            "discount_percent": 56,
            "marketplace": "mercadolibre",
            "affiliate_url": "https://meli.la/abc",
            "url": "https://meli.la/abc",
            "ml_previous_price_verified": True,
            "current_price_verified": True,
            "current_price_raw_text": "$350",
        },
    )
    out = await publisher.publish(item)
    # ML pasa el gate de Amazon (no aplica) y su propio gate (precio verificado).
    assert out.success is True
