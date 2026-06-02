"""Tests de la integración del ScreenshotCapturer con el WhatsAppPublisher.

Verifican que:
- Si el capturer devuelve bytes, el publisher manda ESOS bytes a send_media
  (no la image_url pública).
- Si el capturer devuelve None (captcha/timeout), hace fallback a image_url.
- Si el capturer lanza excepción, NO tumba la publicación (fallback a image_url).
"""

from __future__ import annotations

import pytest

from ofertas_hunter.models import OutboxItem, OutboxType
from ofertas_hunter.publishing.whatsapp_publisher import WhatsAppPublisher


class _SpyClient:
    """Cliente fake que registra lo que se le manda a send_media."""

    dry_run = False

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_media(self, number, media, caption="", *, file_name="oferta.jpg"):
        self.calls.append(
            {"number": number, "media": media, "caption": caption, "file_name": file_name}
        )
        from ofertas_hunter.publishing.evolution_client import EvolutionResponse

        return EvolutionResponse(success=True, status_code=201, raw={})


class _FakeCapturer:
    def __init__(self, result):
        self._result = result
        self.calls = 0

    async def capture(self, payload):
        self.calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _ml_payload() -> dict:
    return {
        "title": "Robot Aspiradora Trapeadora Deerma S30",
        "current_price": 4866,
        "previous_price": 12815,
        "discount_percent": 62,
        "url": "https://meli.la/1iG9VvY",
        "affiliate_url": "https://meli.la/1iG9VvY",
        "canonical_url": "https://www.mercadolibre.com.mx/x/p/MLM54105030",
        "image_url": "https://http2.mlstatic.com/D_NQ_NP_2X_847496.webp",
        "marketplace": "mercadolibre",
        "ml_previous_price_verified": True,
    }


@pytest.mark.asyncio
async def test_publisher_uses_screenshot_bytes_when_capture_succeeds():
    client = _SpyClient()
    shot = b"\xff\xd8\xff\xe0FAKEJPEGBYTES"
    capturer = _FakeCapturer(shot)
    publisher = WhatsAppPublisher(
        client=client,
        target_group_id="120363@g.us",
        enabled=True,
        mercadolibre_affiliate_required=True,
        screenshot_capturer=capturer,
    )
    item = OutboxItem(offer_id=1, type=OutboxType.NORMAL.value, message_payload=_ml_payload())

    result = await publisher.publish(item)

    assert result.success is True
    assert capturer.calls == 1
    assert len(client.calls) == 1
    # Se mandó el screenshot, NO la image_url pública.
    assert client.calls[0]["media"] == shot
    assert client.calls[0]["file_name"] == "oferta_captura.jpg"


@pytest.mark.asyncio
async def test_publisher_falls_back_to_image_url_when_capture_none():
    client = _SpyClient()
    capturer = _FakeCapturer(None)
    publisher = WhatsAppPublisher(
        client=client,
        target_group_id="120363@g.us",
        enabled=True,
        mercadolibre_affiliate_required=True,
        screenshot_capturer=capturer,
    )
    item = OutboxItem(offer_id=2, type=OutboxType.NORMAL.value, message_payload=_ml_payload())

    result = await publisher.publish(item)

    assert result.success is True
    assert capturer.calls == 1
    # Fallback: se mandó la image_url pública.
    assert client.calls[0]["media"] == _ml_payload()["image_url"]
    assert client.calls[0]["file_name"] == "oferta.jpg"


@pytest.mark.asyncio
async def test_publisher_survives_capturer_exception():
    client = _SpyClient()
    capturer = _FakeCapturer(RuntimeError("boom"))
    publisher = WhatsAppPublisher(
        client=client,
        target_group_id="120363@g.us",
        enabled=True,
        mercadolibre_affiliate_required=True,
        screenshot_capturer=capturer,
    )
    item = OutboxItem(offer_id=3, type=OutboxType.NORMAL.value, message_payload=_ml_payload())

    result = await publisher.publish(item)

    assert result.success is True
    # No tumba la publicación; fallback a image_url.
    assert client.calls[0]["media"] == _ml_payload()["image_url"]


@pytest.mark.asyncio
async def test_publisher_no_capturer_uses_image_url():
    client = _SpyClient()
    publisher = WhatsAppPublisher(
        client=client,
        target_group_id="120363@g.us",
        enabled=True,
        mercadolibre_affiliate_required=True,
        screenshot_capturer=None,
    )
    item = OutboxItem(offer_id=4, type=OutboxType.NORMAL.value, message_payload=_ml_payload())

    result = await publisher.publish(item)

    assert result.success is True
    assert client.calls[0]["media"] == _ml_payload()["image_url"]
