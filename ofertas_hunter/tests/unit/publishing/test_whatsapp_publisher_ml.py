"""Tests del WhatsAppPublisher: regla dura de afiliados Mercado Libre."""

from __future__ import annotations

import pytest

from ofertas_hunter.models import OutboxItem, OutboxType
from ofertas_hunter.publishing.evolution_client import EvolutionClient
from ofertas_hunter.publishing.whatsapp_publisher import WhatsAppPublisher


def _ml_payload(*, with_affiliate: bool, type_=OutboxType.NORMAL.value) -> dict:
    payload = {
        "title": "Audífonos Sony WH-1000XM5",
        "current_price": 3499,
        "previous_price": 8999,
        "discount_percent": 61,
        "image_url": "https://http2.mlstatic.com/img.jpg",
        "marketplace": "mercadolibre",
        "canonical_url": "https://articulo.mercadolibre.com.mx/MLM98765432",
        "url": "https://articulo.mercadolibre.com.mx/MLM98765432",
        "confidence_label": "high",
        # Nuevo contrato ML: precio anterior verificado (strikethrough real).
        "ml_previous_price_verified": True,
        "current_price_verified": True,
        "discount_percent_verified": True,
        "current_price_raw_text": "$3,499",
    }
    if with_affiliate:
        payload["affiliate_url"] = "https://meli.la/abc123"
        payload["affiliate_product_id"] = "FAKE-12345"
        payload["commission_text"] = "COMISIÓN 9%"
        payload["url"] = payload["affiliate_url"]
    return payload


@pytest.fixture
def dry_run_client() -> EvolutionClient:
    return EvolutionClient(
        base_url="http://x:8080", api_key="k", instance="i", dry_run=True
    )


@pytest.mark.asyncio
async def test_whatsapp_uses_affiliate_url_for_ml(dry_run_client):
    publisher = WhatsAppPublisher(
        client=dry_run_client,
        target_group_id="120363@g.us",
        enabled=True,
        mercadolibre_affiliate_required=True,
    )
    item = OutboxItem(
        offer_id=1,
        type=OutboxType.NORMAL.value,
        message_payload=_ml_payload(with_affiliate=True),
    )
    result = await publisher.publish(item)
    assert result.success is True
    # El mensaje formateado contiene la URL de afiliado, NO la canónica.
    assert "https://meli.la/abc123" in result.formatted.text
    assert "MLM98765432" not in result.formatted.text


@pytest.mark.asyncio
async def test_whatsapp_blocks_ml_without_affiliate(dry_run_client):
    publisher = WhatsAppPublisher(
        client=dry_run_client,
        target_group_id="120363@g.us",
        enabled=True,
        mercadolibre_affiliate_required=True,
    )
    item = OutboxItem(
        offer_id=2,
        type=OutboxType.NORMAL.value,
        message_payload=_ml_payload(with_affiliate=False),
    )
    result = await publisher.publish(item)
    assert result.success is False
    assert result.error == "missing_affiliate_url"
    # Y NO se llamó a Evolution: la respuesta no se formateó.
    assert result.formatted is None
    assert result.evolution_response is None


@pytest.mark.asyncio
async def test_whatsapp_allows_ml_without_affiliate_when_not_required(dry_run_client):
    publisher = WhatsAppPublisher(
        client=dry_run_client,
        target_group_id="120363@g.us",
        enabled=True,
        mercadolibre_affiliate_required=False,
    )
    item = OutboxItem(
        offer_id=3,
        type=OutboxType.NORMAL.value,
        message_payload=_ml_payload(with_affiliate=False),
    )
    result = await publisher.publish(item)
    assert result.success is True
    # Usa canonical_url cuando affiliate_required=False
    assert "MLM98765432" in result.formatted.text


@pytest.mark.asyncio
async def test_whatsapp_amazon_does_not_require_affiliate(dry_run_client):
    """La regla afiliado de ML solo aplica a marketplace='mercadolibre'.

    Amazon tiene su propio gate (afiliado válido + precio anterior
    verificado). Aquí el item Amazon cumple ambos, así que publica: prueba
    que la regla *de ML* no es la que lo bloquea.
    """
    publisher = WhatsAppPublisher(
        client=dry_run_client,
        target_group_id="120363@g.us",
        enabled=True,
        mercadolibre_affiliate_required=True,
    )
    item = OutboxItem(
        offer_id=4,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "JBL Tune 510BT",
            "current_price": 388,
            "previous_price": 899,
            "discount_percent": 57,
            "image_url": "https://m.media-amazon.com/images/I/abc.jpg",
            "marketplace": "amazon",
            "affiliate_url": "https://amzn.to/4e3yTjG",
            "old_price_verified": True,
            "url": "https://amzn.to/4e3yTjG",
        },
    )
    result = await publisher.publish(item)
    assert result.success is True
