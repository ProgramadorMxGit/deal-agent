"""Tests del PlaywrightRevalidator con FakeBrowserWorker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

from ofertas_hunter.browser.browser_context import RenderedPage
from ofertas_hunter.db import connect, init_db
from ofertas_hunter.dispatching.cooldown import CooldownPolicy
from ofertas_hunter.dispatching.dispatcher import OutboxDispatcher, RevalidationResult
from ofertas_hunter.dispatching.outbox import InMemoryOutbox, OutboxConfig
from ofertas_hunter.models import OutboxItem, OutboxState, OutboxType
from ofertas_hunter.publishing.evolution_client import EvolutionClient
from ofertas_hunter.publishing.whatsapp_publisher import WhatsAppPublisher
from ofertas_hunter.revalidation.playwright_revalidator import PlaywrightRevalidator


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "amazon"
T0 = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeBrowserWorker:
    """BrowserWorker en memoria que devuelve HTML predefinido por URL."""

    def __init__(self, pages: dict[str, RenderedPage]) -> None:
        self.pages = pages
        self.fetched: list[str] = []

    async def fetch(self, url: str) -> RenderedPage:
        self.fetched.append(url)
        if url in self.pages:
            return self.pages[url]
        # Default: response vacía con error para forzar fatal en tests.
        return RenderedPage(url=url, final_url=url, status=0, html="", error="not_mocked")

    async def aclose(self) -> None:  # pragma: no cover
        pass


def _ok_page(html: str, url: str) -> RenderedPage:
    return RenderedPage(url=url, final_url=url, status=200, html=html)


def _captcha_page(url: str) -> RenderedPage:
    return RenderedPage(
        url=url,
        final_url=url,
        status=503,
        html="<html>Enter the characters you see below</html>",
        error="captcha_detected",
        blocked=True,
    )


def _outbox_item_from_telegram(url: str, *, hours_ago: float = 0.5) -> OutboxItem:
    return OutboxItem(
        offer_id=1,
        type=OutboxType.PRICE_ERROR.value,
        message_payload={
            "title": "Apple iPhone 16 Pro Max 256GB",
            "current_price": 3899.0,
            "url": url,
            "image_url": None,
            "marketplace": "amazon",
            "source": "telegram",
            "source_channel": "OFERTAS_RELAMPAGO",
            "original_url": url,
            "resolved_url": url,
            "urgency_terms": ["ERROR DE PRECIO", "CORRAN"],
            "requires_live_validation": True,
        },
        enqueued_at=T0 - timedelta(hours=hours_ago),
    )


# ---------------------------------------------------------------------------
# Tests obligatorios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revalidator_revalidates_telegram_item():
    url = "https://www.amazon.com.mx/dp/B0EXAMPLEI"
    browser = FakeBrowserWorker({url: _ok_page(_load("iphone_extreme_low_price.html"), url)})

    revalidator = PlaywrightRevalidator(browser=browser)
    item = _outbox_item_from_telegram(url, hours_ago=0.1)

    result = await revalidator.revalidate(item)

    assert result.still_eligible is True
    assert result.payload is not None
    assert result.payload["current_price"] == 3899.0
    assert result.payload["previous_price"] == 32999.0
    assert result.payload["image_url"] is not None
    assert result.payload["requires_live_validation"] is False
    assert result.payload["_suggested_outbox_type"] == OutboxType.PRICE_ERROR.value
    assert browser.fetched == [url]


@pytest.mark.asyncio
async def test_revalidator_confirms_price_error_bypass():
    """Tras revalidar, el dispatcher debe poder publicar (bypass cooldown)."""
    url = "https://www.amazon.com.mx/dp/B0EXAMPLEI"
    browser = FakeBrowserWorker({url: _ok_page(_load("iphone_extreme_low_price.html"), url)})
    revalidator = PlaywrightRevalidator(browser=browser)

    outbox = InMemoryOutbox(
        OutboxConfig(
            revalidate_age_seconds=3600,
            cooldown=CooldownPolicy(cooldown_seconds=300),
        )
    )
    # Encolamos un item viejo (>1h) → el dispatcher invoca al revalidator.
    item = _outbox_item_from_telegram(url, hours_ago=2)
    outbox.enqueue(item)

    client = EvolutionClient(base_url="http://x", api_key="k", instance="i", dry_run=True)
    publisher = WhatsAppPublisher(client=client, target_group_id="120363@g.us", enabled=True)
    dispatcher = OutboxDispatcher(
        outbox=outbox,
        publisher=publisher,
        revalidator=revalidator,
        clock=lambda: T0,
    )

    outcome = await dispatcher.tick()

    # Se publicó (dry-run).
    assert outcome is not None
    assert outcome.success is True
    assert outcome.formatted is not None
    # El payload nuevo trajo precio del HTML, no el de Telegram.
    assert "$3,899" in outcome.formatted.text
    # Imagen del HTML (no None).
    assert outcome.formatted.image_url.startswith("https://m.media-amazon.com/")


@pytest.mark.asyncio
async def test_revalidator_confirms_normal_offer_cooldown():
    """Una oferta normal revalidada queda como NORMAL (respeta cooldown)."""
    url = "https://www.amazon.com.mx/dp/B0EXAMPLEK"
    browser = FakeBrowserWorker({url: _ok_page(_load("jbl_normal_offer.html"), url)})
    revalidator = PlaywrightRevalidator(browser=browser)

    item = OutboxItem(
        offer_id=2,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "JBL Tune 510BT",
            "current_price": 388,
            "previous_price": 899,
            "discount_percent": 57,
            "url": url,
            "image_url": "https://m.media-amazon.com/cached.jpg",
            "marketplace": "amazon",
            "source": "amazon_hunter",
        },
        enqueued_at=T0 - timedelta(hours=2),
    )

    detail = await revalidator.revalidate_detailed(item)
    assert detail.ok is True
    assert detail.suggested_outbox_type == OutboxType.NORMAL.value
    # Discount real ≈ 57% → normal
    assert detail.extracted.discount_percent in (57, 57.0, 57.51)


@pytest.mark.asyncio
async def test_revalidator_revalidates_items_older_than_one_hour():
    """El dispatcher invoca revalidator para items >1h antes de publicar."""
    url = "https://www.amazon.com.mx/dp/B0EXAMPLEK"
    browser = FakeBrowserWorker({url: _ok_page(_load("jbl_normal_offer.html"), url)})
    revalidator_calls: list[int] = []

    class TrackingRevalidator:
        async def revalidate(self, item):
            revalidator_calls.append(item.id)
            return await PlaywrightRevalidator(browser=browser).revalidate(item)

    outbox = InMemoryOutbox(
        OutboxConfig(revalidate_age_seconds=3600, cooldown=CooldownPolicy(cooldown_seconds=300))
    )
    item = OutboxItem(
        offer_id=3,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "JBL Tune 510BT",
            "current_price": 388,
            "previous_price": 899,
            "discount_percent": 57,
            "url": url,
            "image_url": "https://m.media-amazon.com/cached.jpg",
            "marketplace": "amazon",
        },
        enqueued_at=T0 - timedelta(hours=2),
    )
    outbox.enqueue(item)

    client = EvolutionClient(base_url="http://x", api_key="k", instance="i", dry_run=True)
    publisher = WhatsAppPublisher(client=client, target_group_id="120363@g.us", enabled=True)
    dispatcher = OutboxDispatcher(
        outbox=outbox,
        publisher=publisher,
        revalidator=TrackingRevalidator(),
        clock=lambda: T0,
    )

    outcome = await dispatcher.tick()
    assert outcome is not None
    assert outcome.success is True
    # El revalidator se llamó porque el item es viejo (1 sola llamada).
    assert len(revalidator_calls) == 1


@pytest.mark.asyncio
async def test_revalidator_discards_expired_offer():
    """Producto sin stock en página real → discard expired."""
    url = "https://www.amazon.com.mx/dp/B0EXAMPLEZ"
    browser = FakeBrowserWorker({url: _ok_page(_load("out_of_stock.html"), url)})
    revalidator = PlaywrightRevalidator(browser=browser)

    item = OutboxItem(
        offer_id=4,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "Sony WH-1000XM5",
            "current_price": 5499,
            "previous_price": 8999,
            "discount_percent": 39,
            "url": url,
            "image_url": "https://m.media-amazon.com/cached.jpg",
            "marketplace": "amazon",
        },
        enqueued_at=T0 - timedelta(hours=2),
    )

    result = await revalidator.revalidate(item)
    assert result.still_eligible is False
    assert result.discard_reason == "out_of_stock"


@pytest.mark.asyncio
async def test_revalidator_handles_captcha():
    """Si el browser detecta captcha, el revalidator descarta."""
    url = "https://www.amazon.com.mx/dp/B0CAPTCHA"
    browser = FakeBrowserWorker({url: _captcha_page(url)})
    revalidator = PlaywrightRevalidator(browser=browser)

    item = OutboxItem(
        offer_id=5,
        type=OutboxType.PRICE_ERROR.value,
        message_payload={
            "title": "X",
            "current_price": 100,
            "url": url,
            "image_url": "x",
            "marketplace": "amazon",
        },
        enqueued_at=T0 - timedelta(hours=2),
    )
    result = await revalidator.revalidate(item)
    assert result.still_eligible is False
    assert "captcha" in (result.discard_reason or "")


@pytest.mark.asyncio
async def test_revalidator_saves_snapshot_when_dom_fails(tmp_path):
    """Cuando la extracción falla, se guarda snapshot HTML en `dom_snapshots`."""
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://www.amazon.com.mx/dp/B0BROKEN"
    browser = FakeBrowserWorker({url: _ok_page(_load("dom_broken.html"), url)})
    revalidator = PlaywrightRevalidator(browser=browser, db_conn=conn)

    item = OutboxItem(
        offer_id=6,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": "X",
            "current_price": 100,
            "url": url,
            "image_url": "x",
            "marketplace": "amazon",
        },
        enqueued_at=T0 - timedelta(hours=2),
    )

    detail = await revalidator.revalidate_detailed(item)
    assert detail.ok is False
    assert detail.snapshot_saved is not None
    rows = conn.execute("SELECT marketplace, context, reason FROM dom_snapshots").fetchall()
    assert len(rows) == 1
    assert rows[0]["marketplace"] == "amazon"
    assert rows[0]["context"] == "revalidator"
    conn.close()
