"""Tests de integración: OutboxDispatcher + ItemSelector.

Cubre Task 4 del plan diversity-curator-agent:

1. Cuando se inyecta un `item_selector`, el dispatcher delega en él la
   elección del próximo item (en vez de `pick_random_eligible`).
2. Cuando `item_selector=None`, el dispatcher mantiene el comportamiento
   legacy (zero-regression): usa `pick_random_eligible`.
3. Si el selector lanza una excepción, el dispatcher hace fallback a
   `pick_random_eligible` y loggea el error (tolerancia a fallos del
   curator/LLM/db en producción).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from ofertas_hunter.dispatching.cooldown import CooldownPolicy
from ofertas_hunter.dispatching.dispatcher import OutboxDispatcher
from ofertas_hunter.dispatching.outbox import InMemoryOutbox, OutboxConfig
from ofertas_hunter.models import OutboxItem, OutboxState, OutboxType
from ofertas_hunter.publishing.whatsapp_publisher import PublishOutcome


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _item(idx: int) -> OutboxItem:
    """Build a normal OutboxItem with a payload that satisfies image/url gates."""
    return OutboxItem(
        offer_id=idx,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": f"Item {idx}",
            "current_price": 100.0,
            "previous_price": 200.0,
            "discount_percent": 50,
            "url": f"https://example/{idx}",
            "image_url": "https://example/img.jpg",
        },
        enqueued_at=_now(),
        attempts=0,
        state=OutboxState.PENDING.value,
    )


class _DummyPublisher:
    """Minimal publisher that records publish() calls and returns success."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def publish(self, item: OutboxItem) -> PublishOutcome:
        self.calls.append(item.id)
        return PublishOutcome(
            success=True,
            dry_run=False,
            formatted=None,
            evolution_response=None,
        )


@pytest.mark.asyncio
async def test_dispatcher_uses_item_selector_when_provided():
    """Dado un selector que devuelve item id=2, el publisher recibe ese item."""
    outbox = InMemoryOutbox(OutboxConfig(cooldown=CooldownPolicy(0)))
    items = [outbox.enqueue(_item(i)) for i in (1, 2, 3)]

    publisher = _DummyPublisher()
    chosen = items[1]  # id=2

    async def selector(_outbox, _last_pub, _now):
        return chosen

    dispatcher = OutboxDispatcher(
        outbox=outbox,
        publisher=publisher,
        item_selector=selector,
    )

    await dispatcher.tick()

    assert publisher.calls == [chosen.id]


@pytest.mark.asyncio
async def test_dispatcher_falls_back_to_pick_random_when_no_selector():
    """Sin selector inyectado, el dispatcher mantiene el path legacy."""
    outbox = InMemoryOutbox(OutboxConfig(cooldown=CooldownPolicy(0)))
    enqueued = outbox.enqueue(_item(1))

    publisher = _DummyPublisher()

    dispatcher = OutboxDispatcher(
        outbox=outbox,
        publisher=publisher,
        item_selector=None,
    )

    await dispatcher.tick()

    # Una sola publicación, correspondiente al único item elegible:
    # confirma que se usó pick_random_eligible vía path legacy.
    assert publisher.calls == [enqueued.id]


@pytest.mark.asyncio
async def test_dispatcher_falls_back_to_pick_random_when_selector_raises(caplog):
    """Si el selector lanza, el dispatcher hace fallback al random pick.

    Crítico para producción: si el curator/LLM/db tienen un fallo
    inesperado, el dispatching debe continuar.
    """
    outbox = InMemoryOutbox(OutboxConfig(cooldown=CooldownPolicy(0)))
    enqueued = outbox.enqueue(_item(1))

    publisher = _DummyPublisher()

    async def selector_raises(_outbox, _last_pub, _now):
        raise RuntimeError("boom")

    dispatcher = OutboxDispatcher(
        outbox=outbox,
        publisher=publisher,
        item_selector=selector_raises,
    )

    with caplog.at_level(logging.ERROR, logger="ofertas_hunter.dispatching.dispatcher"):
        await dispatcher.tick()

    # El item se publicó (fallback funcionó).
    assert publisher.calls == [enqueued.id]

    # Y el error del selector quedó registrado.
    selector_log_records = [
        r for r in caplog.records if "item_selector" in r.getMessage()
    ]
    assert selector_log_records, "Expected an exception log mentioning item_selector"
    assert any(r.exc_info is not None for r in selector_log_records), (
        "Expected logger.exception (with exc_info) for item_selector failure"
    )
