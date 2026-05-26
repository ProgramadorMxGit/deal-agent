"""Tests del PlaywrightRevalidator para Mercado Libre.

Cubre:

- ML del hunter propio: la revalidación conserva affiliate_url y canonical_url
  del payload original, y NO regenera el modal.
- ML que viene marcado como source=telegram: el revalidator devuelve
  not_publishable con razón `mercadolibre_link_from_telegram`, sin parsear
  precio, aunque la cookie y la página estén vivas.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ofertas_hunter.browser.browser_context import RenderedPage
from ofertas_hunter.models import OutboxItem, OutboxType, Source
from ofertas_hunter.revalidation.playwright_revalidator import PlaywrightRevalidator


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "mercadolibre"
T0 = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeBrowserWorker:
    def __init__(self, pages: dict[str, RenderedPage]) -> None:
        self.pages = pages
        self.fetched: list[str] = []

    async def fetch(self, url: str) -> RenderedPage:
        self.fetched.append(url)
        return self.pages.get(
            url,
            RenderedPage(url=url, final_url=url, status=0, html="", error="not_mocked"),
        )

    async def aclose(self) -> None:  # pragma: no cover
        pass


def _ok(html: str, url: str) -> RenderedPage:
    return RenderedPage(url=url, final_url=url, status=200, html=html)


@pytest.mark.asyncio
async def test_ml_revalidator_preserves_affiliate_url():
    """Tras revalidar, el payload mantiene affiliate_url, canonical_url, etc."""
    canonical = "https://articulo.mercadolibre.com.mx/MLM98765432"
    affiliate = "https://meli.la/abc123"
    # El browser puede recibir cualquiera; aquí mockeamos el canonical.
    browser = FakeBrowserWorker({canonical: _ok(_load("extreme_discount_60_percent.html"), canonical)})

    revalidator = PlaywrightRevalidator(browser=browser)

    item = OutboxItem(
        offer_id=10,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "Audífonos Sony WH-1000XM5",
            "current_price": 3499,
            "previous_price": 8999,
            "discount_percent": 61,
            "image_url": "https://cached.jpg",
            "url": affiliate,
            "canonical_url": canonical,
            # `resolved_url` indica al revalidator: "para fetchear, usa esta URL"
            # (el shortlink afiliado meli.la podría no resolver si la cookie cambia).
            "resolved_url": canonical,
            "affiliate_url": affiliate,
            "affiliate_product_id": "FAKE-12345",
            "commission_text": "COMISIÓN 9%",
            "marketplace": "mercadolibre",
            "source": Source.MERCADOLIBRE_HUNTER.value,
        },
        enqueued_at=T0 - timedelta(hours=2),
    )

    result = await revalidator.revalidate(item)
    assert result.still_eligible is True
    assert result.payload is not None
    # Affiliate metadata preservada del payload original.
    assert result.payload["affiliate_url"] == affiliate
    assert result.payload["affiliate_product_id"] == "FAKE-12345"
    assert result.payload["commission_text"] == "COMISIÓN 9%"
    # url (publicación) sigue siendo affiliate_url; canonical_url actualizado
    # con lo que extrajo el parser.
    assert result.payload["url"] == affiliate
    assert result.payload["canonical_url"] == canonical
    # El revalidator fetcheó por resolved_url (canonical), no por affiliate.
    assert browser.fetched == [canonical]


@pytest.mark.asyncio
async def test_ml_revalidator_uses_canonical_for_fetch_when_available():
    """Si url == affiliate_url, el revalidator igual debería fetchear vía
    `resolved_url` o `canonical_url` para no quemar shortlinks afiliados.

    Reglas actuales del revalidator: `_url_to_fetch` usa
    `resolved_url -> url -> original_url`. Aquí mostramos que si añadimos
    `resolved_url=canonical`, el revalidator fetchea por ahí.
    """
    canonical = "https://articulo.mercadolibre.com.mx/MLM98765432"
    affiliate = "https://meli.la/abc123"
    browser = FakeBrowserWorker({canonical: _ok(_load("extreme_discount_60_percent.html"), canonical)})

    revalidator = PlaywrightRevalidator(browser=browser)

    item = OutboxItem(
        offer_id=10,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "Audífonos Sony WH-1000XM5",
            "current_price": 3499,
            "previous_price": 8999,
            "discount_percent": 61,
            "image_url": "https://cached.jpg",
            "url": affiliate,
            "resolved_url": canonical,
            "canonical_url": canonical,
            "affiliate_url": affiliate,
            "affiliate_product_id": "FAKE-12345",
            "marketplace": "mercadolibre",
            "source": Source.MERCADOLIBRE_HUNTER.value,
        },
        enqueued_at=T0 - timedelta(hours=2),
    )

    result = await revalidator.revalidate(item)
    assert result.still_eligible is True
    assert browser.fetched == [canonical]


@pytest.mark.asyncio
async def test_ml_revalidator_blocks_telegram_source():
    """ML cuyo source es 'telegram' nunca se valida ni se publica."""
    url = "https://articulo.mercadolibre.com.mx/MLM98765432"
    browser = FakeBrowserWorker({url: _ok(_load("extreme_discount_60_percent.html"), url)})
    revalidator = PlaywrightRevalidator(browser=browser)

    item = OutboxItem(
        offer_id=11,
        type=OutboxType.PRICE_ERROR.value,
        message_payload={
            "title": "Tablet Samsung",
            "current_price": 2499,
            "url": url,
            "canonical_url": url,
            "image_url": "https://x.com/img.jpg",
            "marketplace": "mercadolibre",
            "source": Source.TELEGRAM.value,  # ← origen Telegram
        },
        enqueued_at=T0 - timedelta(hours=2),
    )

    result = await revalidator.revalidate(item)
    assert result.still_eligible is False
    assert result.discard_reason == "mercadolibre_link_from_telegram"


@pytest.mark.asyncio
async def test_ml_revalidator_confirms_normal_offer():
    canonical = "https://articulo.mercadolibre.com.mx/MLM98765432"
    browser = FakeBrowserWorker({canonical: _ok(_load("extreme_discount_60_percent.html"), canonical)})
    revalidator = PlaywrightRevalidator(browser=browser)

    item = OutboxItem(
        offer_id=12,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "Sony WH-1000XM5",
            "current_price": 3499,
            "previous_price": 8999,
            "discount_percent": 61,
            "url": canonical,
            "canonical_url": canonical,
            "affiliate_url": "https://meli.la/abc",
            "image_url": "x",
            "marketplace": "mercadolibre",
            "source": Source.MERCADOLIBRE_HUNTER.value,
        },
        enqueued_at=T0 - timedelta(hours=2),
    )
    detail = await revalidator.revalidate_detailed(item)
    assert detail.ok is True
    assert detail.suggested_outbox_type in ("normal", "price_error", "possible_pe")
