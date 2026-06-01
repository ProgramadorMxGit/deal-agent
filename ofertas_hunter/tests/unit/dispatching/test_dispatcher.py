"""Tests del OutboxDispatcher (Fase 3.1).

Cubre los criterios obligatorios:

- test_dispatcher_does_not_publish_without_image
- test_dispatcher_does_not_publish_without_validated_price
- test_dispatcher_normal_offer_respects_cooldown
- test_dispatcher_price_error_bypasses_cooldown
- test_dispatcher_revalidates_offer_older_than_one_hour
- test_dispatcher_discards_expired_after_revalidation
- test_dispatcher_records_evolution_api_failure
- test_dispatcher_random_selects_among_eligible_normal_offers
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import logging
import sqlite3
from typing import Optional

import pytest

from ofertas_hunter.dispatching.cooldown import CooldownPolicy
from ofertas_hunter.dispatching.dispatcher import (
    NullRevalidator,
    OutboxDispatcher,
    RevalidationResult,
    Revalidator,
)
from ofertas_hunter.dispatching.outbox import InMemoryOutbox, OutboxConfig
from ofertas_hunter.models import OutboxItem, OutboxState, OutboxType
from ofertas_hunter.publishing.evolution_client import EvolutionClient
from ofertas_hunter.publishing.whatsapp_publisher import (
    PublishOutcome,
    WhatsAppPublisher,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


T0 = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _normal_payload() -> dict:
    return {
        "title": "JBL Tune 510BT",
        "current_price": 388,
        "previous_price": 899,
        "discount_percent": 57,
        "url": "https://amzn.to/x",
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


def _make_dispatcher(
    *,
    enabled: bool = True,
    dry_run: bool = True,
    revalidator: Optional[Revalidator] = None,
    cooldown_seconds: int = 300,
    revalidate_age_seconds: int = 3600,
    revalidation_budget_per_tick: int = 2,
    max_attempts_per_tick: int = 10,
    clock=None,
) -> tuple[OutboxDispatcher, InMemoryOutbox]:
    outbox = InMemoryOutbox(
        OutboxConfig(
            revalidate_age_seconds=revalidate_age_seconds,
            cooldown=CooldownPolicy(cooldown_seconds=cooldown_seconds),
        )
    )
    client = EvolutionClient(
        base_url="http://x:8080", api_key="k", instance="i", dry_run=dry_run
    )
    publisher = WhatsAppPublisher(client=client, target_group_id="120363@g.us", enabled=enabled)
    dispatcher = OutboxDispatcher(
        outbox=outbox,
        publisher=publisher,
        revalidator=revalidator or NullRevalidator(),
        revalidation_budget_per_tick=revalidation_budget_per_tick,
        max_attempts_per_tick=max_attempts_per_tick,
        clock=clock or (lambda: T0),
    )
    return dispatcher, outbox


def _enqueue(
    outbox: InMemoryOutbox,
    item_type: str,
    *,
    offer_id: int = 1,
    payload: Optional[dict] = None,
    enqueued_at: Optional[datetime] = None,
) -> OutboxItem:
    item = OutboxItem(
        offer_id=offer_id,
        type=item_type,
        message_payload=payload if payload is not None else _normal_payload(),
        enqueued_at=enqueued_at or T0,
    )
    return outbox.enqueue(item)


# ---------------------------------------------------------------------------
# Tests obligatorios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_does_not_publish_without_image():
    dispatcher, outbox = _make_dispatcher()
    payload = _normal_payload()
    payload["image_url"] = ""
    item = _enqueue(outbox, OutboxType.NORMAL.value, payload=payload)

    outcome = await dispatcher.tick()

    assert outcome is not None
    assert outcome.success is False
    assert "image_url" in (outcome.error or "")

    # El item se marca como FAILED (no publicado) y no consume cooldown.
    persisted = next(i for i in outbox if i.id == item.id)
    assert persisted.state == OutboxState.FAILED.value


@pytest.mark.asyncio
async def test_dispatcher_does_not_publish_without_validated_price():
    dispatcher, outbox = _make_dispatcher()
    payload = _normal_payload()
    payload["current_price"] = None
    item = _enqueue(outbox, OutboxType.NORMAL.value, payload=payload)

    outcome = await dispatcher.tick()
    assert outcome is not None
    assert outcome.success is False
    assert "current_price" in (outcome.error or "")
    persisted = next(i for i in outbox if i.id == item.id)
    assert persisted.state == OutboxState.FAILED.value


@pytest.mark.asyncio
async def test_dispatcher_normal_offer_respects_cooldown():
    """Tras publicar una oferta normal, otra normal no se publica antes de 5min."""
    times = [T0]
    dispatcher, outbox = _make_dispatcher(clock=lambda: times[-1])

    _enqueue(outbox, OutboxType.NORMAL.value, offer_id=1)
    _enqueue(outbox, OutboxType.NORMAL.value, offer_id=2)

    first = await dispatcher.tick()
    assert first is not None and first.success is True

    # Avanzamos 60s. La segunda normal NO debe publicarse aún.
    times.append(T0 + timedelta(seconds=60))
    second = await dispatcher.tick()
    assert second is None  # no hay nada elegible

    # Pasados 5min, sí.
    times.append(T0 + timedelta(seconds=301))
    third = await dispatcher.tick()
    assert third is not None and third.success is True


@pytest.mark.asyncio
async def test_dispatcher_price_error_bypasses_cooldown():
    """Un PE puede publicarse aunque acabe de salir una oferta normal."""
    times = [T0]
    dispatcher, outbox = _make_dispatcher(clock=lambda: times[-1])

    _enqueue(outbox, OutboxType.NORMAL.value, offer_id=1)
    _enqueue(
        outbox,
        OutboxType.PRICE_ERROR.value,
        offer_id=2,
        payload=_price_error_payload(),
    )

    # 1er tick publica el PE (prioridad sobre normales).
    first = await dispatcher.tick()
    assert first is not None
    assert first.success is True
    assert first.formatted.type == "price_error"

    # 2º tick (mismo instante, sin cooldown vs normal aún) publica la normal.
    times.append(T0)
    second = await dispatcher.tick()
    assert second is not None
    assert second.success is True
    assert second.formatted.type == "normal"


@pytest.mark.asyncio
async def test_dispatcher_revalidates_required_live_validation_before_publish():
    """Items requires_live_validation pasan por revalidator antes de publicar."""
    revalidator_calls: list[int] = []

    class TrackingRevalidator:
        async def revalidate(self, item):
            revalidator_calls.append(item.id)
            new_payload = dict(item.message_payload)
            new_payload["current_price"] = 350  # cambia el precio
            new_payload["discount_percent"] = 61
            new_payload["requires_live_validation"] = False
            return RevalidationResult(still_eligible=True, payload=new_payload)

    dispatcher, outbox = _make_dispatcher(
        revalidator=TrackingRevalidator(),
        clock=lambda: T0 + timedelta(hours=2),
    )
    item = _enqueue(
        outbox,
        OutboxType.NORMAL.value,
        offer_id=1,
        enqueued_at=T0,
        payload={**_normal_payload(), "requires_live_validation": True},
    )

    outcome = await dispatcher.tick()

    assert revalidator_calls == [item.id]
    assert outcome is not None and outcome.success is True
    # El payload nuevo se aplicó y se ve reflejado en el mensaje formateado.
    assert "AHORA: $350" in outcome.formatted.text


@pytest.mark.asyncio
async def test_dispatcher_limits_revalidation_work_per_tick_and_still_publishes():
    revalidator_calls: list[int] = []

    class TrackingRevalidator:
        async def revalidate(self, item):
            revalidator_calls.append(item.id)
            payload = dict(item.message_payload)
            payload["requires_live_validation"] = False
            return RevalidationResult(still_eligible=True, payload=payload)

    dispatcher, outbox = _make_dispatcher(
        revalidator=TrackingRevalidator(),
        revalidate_age_seconds=3600,
        revalidation_budget_per_tick=1,
        clock=lambda: T0 + timedelta(hours=2),
    )
    _enqueue(
        outbox,
        OutboxType.NORMAL.value,
        offer_id=1,
        enqueued_at=T0,
        payload={**_normal_payload(), "requires_live_validation": True},
    )
    _enqueue(
        outbox,
        OutboxType.NORMAL.value,
        offer_id=2,
        enqueued_at=T0,
        payload={**_normal_payload(), "requires_live_validation": True},
    )

    outcome = await dispatcher.tick()

    assert len(revalidator_calls) == 1
    assert outcome is not None and outcome.success is True


@pytest.mark.asyncio
async def test_dispatcher_prioritizes_fresh_eligible_item_before_stale_revalidation():
    revalidator_calls: list[int] = []

    class TrackingRevalidator:
        async def revalidate(self, item):
            revalidator_calls.append(item.id)
            return RevalidationResult(still_eligible=True, payload=item.message_payload)

    dispatcher, outbox = _make_dispatcher(
        revalidator=TrackingRevalidator(),
        revalidate_age_seconds=3600,
        clock=lambda: T0 + timedelta(hours=2),
    )
    _enqueue(outbox, OutboxType.NORMAL.value, offer_id=1, enqueued_at=T0)
    _enqueue(
        outbox,
        OutboxType.NORMAL.value,
        offer_id=2,
        enqueued_at=T0 + timedelta(hours=2),
    )

    outcome = await dispatcher.tick()

    assert outcome is not None and outcome.success is True
    assert revalidator_calls == []


@pytest.mark.asyncio
async def test_dispatcher_publishes_stale_non_live_item_without_blocking_on_revalidation():
    revalidator_calls: list[int] = []

    class TrackingRevalidator:
        async def revalidate(self, item):
            revalidator_calls.append(item.id)
            return RevalidationResult(still_eligible=True, payload=item.message_payload)

    dispatcher, outbox = _make_dispatcher(
        revalidator=TrackingRevalidator(),
        revalidate_age_seconds=3600,
        clock=lambda: T0 + timedelta(hours=2),
    )
    item = _enqueue(
        outbox,
        OutboxType.NORMAL.value,
        offer_id=1,
        enqueued_at=T0,
        payload={**_normal_payload(), "requires_live_validation": False},
    )

    outcome = await dispatcher.tick()

    assert outcome is not None and outcome.success is True
    assert revalidator_calls == []
    assert next(i for i in outbox if i.id == item.id).state == OutboxState.SENT.value


@pytest.mark.asyncio
async def test_dispatcher_discards_expired_after_revalidation():
    """Si la revalidación dice still_eligible=False, item queda discarded."""

    class ExpireRevalidator:
        async def revalidate(self, item):
            return RevalidationResult(
                still_eligible=False, discard_reason="discount_below_50"
            )

    dispatcher, outbox = _make_dispatcher(
        revalidator=ExpireRevalidator(),
        clock=lambda: T0 + timedelta(hours=2),
    )
    item = _enqueue(
        outbox,
        OutboxType.NORMAL.value,
        offer_id=1,
        enqueued_at=T0,
        payload={**_normal_payload(), "requires_live_validation": True},
    )

    outcome = await dispatcher.tick()

    # No se publicó nada en este tick (no quedan items elegibles tras descarte).
    assert outcome is None

    persisted = next(i for i in outbox if i.id == item.id)
    assert persisted.state == OutboxState.DISCARDED.value
    assert persisted.message_payload.get("discarded_reason") == "discount_below_50"


@pytest.mark.asyncio
async def test_dispatcher_records_evolution_api_failure():
    """Si Evolution responde con error, item queda como FAILED."""
    import httpx
    from ofertas_hunter.publishing.evolution_client import EvolutionClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as session:
        evo = EvolutionClient(
            base_url="http://x:8080",
            api_key="k",
            instance="i",
            dry_run=False,
            client=session,
        )
        publisher = WhatsAppPublisher(
            client=evo, target_group_id="120363@g.us", enabled=True
        )
        outbox = InMemoryOutbox()
        dispatcher = OutboxDispatcher(outbox=outbox, publisher=publisher, clock=lambda: T0)

        item = _enqueue(outbox, OutboxType.NORMAL.value)
        outcome = await dispatcher.tick()

    assert outcome is not None
    assert outcome.success is False
    assert outcome.evolution_response is not None
    assert outcome.evolution_response.status_code == 500

    persisted = next(i for i in outbox if i.id == item.id)
    assert persisted.state == OutboxState.FAILED.value


@pytest.mark.asyncio
async def test_dispatcher_requeues_temporary_evolution_failure_with_backoff():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "status": 500,
                "error": "Internal Server Error",
                "response": {"message": ["Error: Connection Closed"]},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as session:
        evo = EvolutionClient(
            base_url="http://x:8080",
            api_key="k",
            instance="i",
            dry_run=False,
            client=session,
        )
        publisher = WhatsAppPublisher(
            client=evo, target_group_id="120363@g.us", enabled=True
        )
        outbox = InMemoryOutbox(OutboxConfig(cooldown=CooldownPolicy(0)))
        dispatcher = OutboxDispatcher(outbox=outbox, publisher=publisher, clock=lambda: T0)

        item = _enqueue(outbox, OutboxType.NORMAL.value)
        outcome = await dispatcher.tick()

    assert outcome is not None
    assert outcome.success is False
    assert outcome.evolution_response is not None
    assert outcome.evolution_response.temporary is True

    persisted = next(i for i in outbox if i.id == item.id)
    assert persisted.state == OutboxState.PENDING.value
    assert persisted.attempts == 1
    assert persisted.last_attempt_at == T0
    assert persisted.scheduled_for is not None
    assert persisted.scheduled_for > T0


@pytest.mark.asyncio
async def test_dispatcher_random_selects_among_eligible_normal_offers():
    """Con varias ofertas normales elegibles, el dispatcher las elige aleatoriamente."""
    times = [T0]
    dispatcher, outbox = _make_dispatcher(clock=lambda: times[-1])
    # 3 ofertas normales distintas
    payload1 = _normal_payload()
    payload2 = {**_normal_payload(), "title": "Producto B", "url": "https://amzn.to/b"}
    payload3 = {**_normal_payload(), "title": "Producto C", "url": "https://amzn.to/c"}
    _enqueue(outbox, OutboxType.NORMAL.value, offer_id=1, payload=payload1)
    _enqueue(outbox, OutboxType.NORMAL.value, offer_id=2, payload=payload2)
    _enqueue(outbox, OutboxType.NORMAL.value, offer_id=3, payload=payload3)

    # Forzamos seed para tener resultados deterministas en el rng del outbox.
    outbox._rng.seed(42)

    seen: list[int] = []
    previously_sent: set[int] = set()
    for i in range(3):
        # Avanzamos 6 minutos cada vez para limpiar cooldown.
        times.append(T0 + timedelta(minutes=6 * (i + 1)))
        outcome = await dispatcher.tick()
        assert outcome is not None and outcome.success is True
        sent_now = {it.offer_id for it in outbox if it.state == OutboxState.SENT.value}
        new_in_this_tick = sent_now - previously_sent
        assert len(new_in_this_tick) == 1
        seen.append(next(iter(new_in_this_tick)))
        previously_sent = sent_now

    counter = Counter(seen)
    assert set(counter.keys()) == {1, 2, 3}
    assert sum(counter.values()) == 3


@pytest.mark.asyncio
async def test_dispatcher_publishing_disabled_keeps_item_pending():
    dispatcher, outbox = _make_dispatcher(enabled=False)
    item = _enqueue(outbox, OutboxType.NORMAL.value)

    outcome = await dispatcher.tick()
    assert outcome is not None
    assert outcome.skipped is True

    persisted = next(i for i in outbox if i.id == item.id)
    assert persisted.state == OutboxState.PENDING.value


@pytest.mark.asyncio
async def test_dispatcher_logs_and_falls_back_when_item_selector_fails(caplog: pytest.LogCaptureFixture):
    dispatcher, outbox = _make_dispatcher()
    _enqueue(outbox, OutboxType.NORMAL.value)

    async def broken_selector(*args, **kwargs):
        raise RuntimeError("curator boom")

    dispatcher._item_selector = broken_selector  # noqa: SLF001 - test hook

    with caplog.at_level(logging.WARNING):
        outcome = await dispatcher.tick()

    assert outcome is not None
    assert outcome.success is True
    assert "DiversityCurator failed, falling back to legacy selector" in caplog.text


@pytest.mark.asyncio
async def test_dispatcher_survives_database_lock_during_revalidation_update(
    caplog: pytest.LogCaptureFixture,
):
    class LockingOutbox(InMemoryOutbox):
        def mark_discarded(self, item, reason):
            raise sqlite3.OperationalError("database is locked")

    class ExpireRevalidator:
        async def revalidate(self, item):
            return RevalidationResult(
                still_eligible=False,
                discard_reason="discount_below_50",
            )

    outbox = LockingOutbox()
    item = OutboxItem(
        offer_id=1,
        type=OutboxType.NORMAL.value,
        enqueued_at=T0 - timedelta(hours=2),
        message_payload={
            **_normal_payload(),
            "requires_live_validation": True,
        },
    )
    outbox.enqueue(item)
    client = EvolutionClient(
        base_url="http://x:8080",
        api_key="k",
        instance="i",
        dry_run=True,
    )
    publisher = WhatsAppPublisher(
        client=client,
        target_group_id="120363@g.us",
        enabled=True,
    )
    dispatcher = OutboxDispatcher(
        outbox=outbox,
        publisher=publisher,
        revalidator=ExpireRevalidator(),
        clock=lambda: T0,
    )

    with caplog.at_level(logging.WARNING):
        outcome = await dispatcher.tick()

    assert outcome is None
    assert "database is locked" in caplog.text



# ---------------------------------------------------------------------------
# Tests de hibernación / scheduler
# ---------------------------------------------------------------------------


from ofertas_hunter.runtime.scheduler import (
    OperatingScheduler,
    ScheduleConfig,
    ScheduleMode,
)


def _scheduler_in_mode(mode: ScheduleMode) -> OperatingScheduler:
    """Devuelve un scheduler stub que siempre reporta `mode`."""

    class _StubScheduler(OperatingScheduler):
        def __init__(self, m: ScheduleMode):
            super().__init__()
            self._stub_mode = m

        def decide(self, *, at=None):
            from datetime import time, timedelta
            from ofertas_hunter.runtime.scheduler import ModeDecision

            return ModeDecision(
                mode=self._stub_mode,
                local_time=time(0, 0),
                next_change_in=timedelta(hours=1),
                next_mode=ScheduleMode.ACTIVE,
            )

        def is_active(self) -> bool:
            return self._stub_mode == ScheduleMode.ACTIVE

    return _StubScheduler(mode)


@pytest.mark.asyncio
async def test_dispatcher_hibernates_no_publishing():
    """Durante hibernación, el dispatcher no toca el outbox aunque haya items."""
    times = [T0]
    dispatcher, outbox = _make_dispatcher(clock=lambda: times[-1])
    dispatcher.scheduler = _scheduler_in_mode(ScheduleMode.HIBERNATING)
    _enqueue(outbox, OutboxType.PRICE_ERROR.value, payload=_price_error_payload())
    _enqueue(outbox, OutboxType.NORMAL.value)

    outcome = await dispatcher.tick()
    assert outcome is None

    # Ni el revalidator ni el publisher se invocaron.
    persisted_states = {it.state for it in outbox}
    assert persisted_states == {OutboxState.PENDING.value}


@pytest.mark.asyncio
async def test_dispatcher_warmup_no_publishing():
    """Durante warmup tampoco publicamos: acumulamos para 7am."""
    dispatcher, outbox = _make_dispatcher()
    dispatcher.scheduler = _scheduler_in_mode(ScheduleMode.WARMUP)
    _enqueue(outbox, OutboxType.NORMAL.value)
    _enqueue(outbox, OutboxType.PRICE_ERROR.value, payload=_price_error_payload())

    outcome = await dispatcher.tick()
    assert outcome is None
    assert all(it.state == OutboxState.PENDING.value for it in outbox)


@pytest.mark.asyncio
async def test_dispatcher_active_publishes_normally():
    """En modo activo, el dispatcher funciona como siempre."""
    dispatcher, outbox = _make_dispatcher()
    dispatcher.scheduler = _scheduler_in_mode(ScheduleMode.ACTIVE)
    _enqueue(outbox, OutboxType.PRICE_ERROR.value, payload=_price_error_payload())

    outcome = await dispatcher.tick()
    assert outcome is not None
    assert outcome.success is True


@pytest.mark.asyncio
async def test_dispatcher_no_scheduler_means_always_active():
    """Sin scheduler inyectado, comportamiento legacy = siempre publica."""
    dispatcher, outbox = _make_dispatcher()
    assert dispatcher.scheduler is None
    _enqueue(outbox, OutboxType.NORMAL.value)
    outcome = await dispatcher.tick()
    assert outcome is not None
    assert outcome.success is True



# ---------------------------------------------------------------------------
# Anti-duplicados a nivel dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_skips_duplicate_via_checker(monkeypatch):
    """El duplicate_checker descarta items ya publicados antes de
    invocar al publisher.
    """
    from datetime import datetime, timezone

    from ofertas_hunter.dispatching.dispatcher import OutboxDispatcher
    from ofertas_hunter.dispatching.outbox import (
        InMemoryOutbox,
        OutboxConfig,
    )
    from ofertas_hunter.dispatching.cooldown import CooldownPolicy
    from ofertas_hunter.models import OutboxItem, OutboxState, OutboxType

    outbox = InMemoryOutbox(OutboxConfig(cooldown=CooldownPolicy(0)))
    item = outbox.enqueue(
        OutboxItem(
            offer_id=1,
            type=OutboxType.NORMAL.value,
            enqueued_at=datetime.now(timezone.utc),
            attempts=0,
            state=OutboxState.PENDING.value,
            message_payload={"item_id": "MLM999", "url": "https://x", "image_url": "i"},
        )
    )

    publish_calls: list = []

    class DummyPublisher:
        async def publish(self, item):
            publish_calls.append(item.id)
            from ofertas_hunter.publishing.whatsapp_publisher import (
                PublishOutcome,
            )

            return PublishOutcome(
                success=True,
                dry_run=False,
                formatted=None,
                evolution_response=None,
            )

    def is_dup(item):
        return item.message_payload.get("item_id") == "MLM999"

    dispatcher = OutboxDispatcher(
        outbox=outbox,
        publisher=DummyPublisher(),
        duplicate_checker=is_dup,
    )

    outcome = await dispatcher.tick()
    assert outcome is not None
    assert outcome.skipped
    assert outcome.skip_reason == "recent_duplicate"
    # publisher NO fue invocado
    assert publish_calls == []
    # outbox marcado como discarded
    refreshed = next(i for i in outbox._items if i.id == item.id)  # noqa: SLF001
    assert refreshed.state == OutboxState.DISCARDED.value


@pytest.mark.asyncio
async def test_dispatcher_continues_same_tick_after_duplicate_discard():
    dispatcher, outbox = _make_dispatcher(cooldown_seconds=0)
    duplicate_item = _enqueue(
        outbox,
        OutboxType.NORMAL.value,
        offer_id=1,
        payload={**_normal_payload(), "asin": "DUP-1", "title": "Duplicado"},
    )
    publishable_item = _enqueue(
        outbox,
        OutboxType.NORMAL.value,
        offer_id=2,
        payload={**_normal_payload(), "asin": "OK-2", "title": "Publicable"},
    )

    def is_dup(item):
        return (item.message_payload or {}).get("asin") == "DUP-1"

    dispatcher._is_duplicate = is_dup  # noqa: SLF001 - test hook

    async def ordered_selector(pool, _last_normal_publication_at, now):
        eligible = pool.eligible_now(None, now)
        return eligible[0] if eligible else None

    dispatcher._item_selector = ordered_selector  # noqa: SLF001 - test hook

    outcome = await dispatcher.tick()

    assert outcome is not None
    assert outcome.success is True
    persisted = {item.id: item for item in outbox}
    assert persisted[duplicate_item.id].state == OutboxState.DISCARDED.value
    assert persisted[publishable_item.id].state == OutboxState.SENT.value


@pytest.mark.asyncio
async def test_dispatcher_continues_same_tick_after_item_local_failure():
    dispatcher, outbox = _make_dispatcher(cooldown_seconds=0)
    bad_payload = _normal_payload()
    bad_payload["image_url"] = ""
    bad_item = _enqueue(
        outbox,
        OutboxType.NORMAL.value,
        offer_id=1,
        payload=bad_payload,
    )
    good_item = _enqueue(
        outbox,
        OutboxType.NORMAL.value,
        offer_id=2,
        payload={**_normal_payload(), "asin": "OK-LOCAL", "title": "Oferta válida"},
    )

    async def ordered_selector(pool, _last_normal_publication_at, now):
        eligible = pool.eligible_now(None, now)
        return eligible[0] if eligible else None

    dispatcher._item_selector = ordered_selector  # noqa: SLF001 - test hook

    outcome = await dispatcher.tick()

    assert outcome is not None
    assert outcome.success is True
    persisted = {item.id: item for item in outbox}
    assert persisted[bad_item.id].state == OutboxState.FAILED.value
    assert persisted[good_item.id].state == OutboxState.SENT.value


@pytest.mark.asyncio
async def test_dispatcher_stops_same_tick_on_global_publish_failure():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as session:
        evo = EvolutionClient(
            base_url="http://x:8080",
            api_key="k",
            instance="i",
            dry_run=False,
            client=session,
        )
        publisher = WhatsAppPublisher(
            client=evo, target_group_id="120363@g.us", enabled=True
        )
        outbox = InMemoryOutbox(OutboxConfig(cooldown=CooldownPolicy(0)))
        dispatcher = OutboxDispatcher(
            outbox=outbox,
            publisher=publisher,
            max_attempts_per_tick=5,
            clock=lambda: T0,
        )

        first = _enqueue(
            outbox,
            OutboxType.NORMAL.value,
            offer_id=1,
            payload={**_normal_payload(), "asin": "HTTP-1", "title": "HTTP fail"},
        )
        second = _enqueue(
            outbox,
            OutboxType.NORMAL.value,
            offer_id=2,
            payload={**_normal_payload(), "asin": "HTTP-2", "title": "No debe intentarse"},
        )

        async def ordered_selector(pool, _last_normal_publication_at, now):
            eligible = pool.eligible_now(None, now)
            return eligible[0] if eligible else None

        dispatcher._item_selector = ordered_selector  # noqa: SLF001 - test hook

        outcome = await dispatcher.tick()

    assert outcome is not None
    assert outcome.success is False
    persisted = {item.id: item for item in outbox}
    assert persisted[first.id].state == OutboxState.FAILED.value
    assert persisted[second.id].state == OutboxState.PENDING.value
