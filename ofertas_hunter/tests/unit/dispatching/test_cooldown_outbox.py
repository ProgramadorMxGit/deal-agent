"""Tests de cooldown y outbox.

Cubre los criterios obligatorios de la spec §16:
- test_normal_offer_respects_5_min_cooldown
- test_price_error_bypasses_cooldown
- test_outbox_revalidates_after_one_hour
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ofertas_hunter.dispatching.cooldown import CooldownPolicy
from ofertas_hunter.dispatching.outbox import InMemoryOutbox, OutboxConfig
from ofertas_hunter.models import OutboxItem, OutboxState, OutboxType


def _now() -> datetime:
    return datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _item(item_type: str, offer_id: int = 1, enqueued_at: datetime | None = None) -> OutboxItem:
    return OutboxItem(
        offer_id=offer_id,
        type=item_type,
        message_payload={"title": "X", "current_price": 100, "url": "https://x.com/p"},
        enqueued_at=enqueued_at or _now(),
    )


class TestCooldownPolicy:
    def test_normal_offer_respects_5_min_cooldown(self):
        policy = CooldownPolicy(cooldown_seconds=300)
        last = _now()

        # Justo después: no publicable
        assert (
            policy.is_publishable(
                OutboxType.NORMAL.value,
                last_normal_publication_at=last,
                now=last + timedelta(seconds=10),
            )
            is False
        )
        # 4 min después: aún no
        assert (
            policy.is_publishable(
                OutboxType.NORMAL.value,
                last_normal_publication_at=last,
                now=last + timedelta(minutes=4),
            )
            is False
        )
        # 5 min después: sí
        assert (
            policy.is_publishable(
                OutboxType.NORMAL.value,
                last_normal_publication_at=last,
                now=last + timedelta(minutes=5),
            )
            is True
        )

    def test_price_error_bypasses_cooldown(self):
        policy = CooldownPolicy(cooldown_seconds=300)
        last = _now()

        # Sin importar si hay cooldown activo, error de precio publica.
        assert (
            policy.is_publishable(
                OutboxType.PRICE_ERROR.value,
                last_normal_publication_at=last,
                now=last + timedelta(seconds=1),
            )
            is True
        )

        # Possible_pe también bypass (tras revalidación inmediata).
        assert (
            policy.is_publishable(
                OutboxType.POSSIBLE_PE.value,
                last_normal_publication_at=last,
                now=last,
            )
            is True
        )

    def test_no_previous_publication_allows_immediate(self):
        policy = CooldownPolicy(cooldown_seconds=300)
        assert policy.is_publishable(OutboxType.NORMAL.value, last_normal_publication_at=None) is True


class TestInMemoryOutbox:
    def test_enqueue_and_pending(self):
        outbox = InMemoryOutbox()
        outbox.enqueue(_item(OutboxType.NORMAL.value))
        outbox.enqueue(_item(OutboxType.PRICE_ERROR.value, offer_id=2))
        assert len(outbox) == 2
        assert len(outbox.pending()) == 2

    def test_outbox_revalidates_after_one_hour(self):
        """Items con > 1h en outbox aparecen en needs_revalidation."""
        outbox = InMemoryOutbox(OutboxConfig(revalidate_age_seconds=3600))
        old = _item(OutboxType.NORMAL.value, offer_id=1, enqueued_at=_now() - timedelta(hours=2))
        fresh = _item(OutboxType.NORMAL.value, offer_id=2, enqueued_at=_now() - timedelta(minutes=10))
        outbox.enqueue(old)
        outbox.enqueue(fresh)

        needs = outbox.needs_revalidation(now=_now())
        assert len(needs) == 1
        assert needs[0].offer_id == 1

    def test_pick_random_eligible_prefers_price_error(self):
        outbox = InMemoryOutbox()
        for i in range(5):
            outbox.enqueue(_item(OutboxType.NORMAL.value, offer_id=i + 1))
        outbox.enqueue(_item(OutboxType.PRICE_ERROR.value, offer_id=99))

        # Con 5 normales y 1 PE, repetidamente se elige el PE primero.
        # Ejecutamos 30 picks: todos deben ser PE (porque _now=last_normal=None y
        # con cooldown no aplica).
        picks: list[int] = []
        for _ in range(30):
            chosen = outbox.pick_random_eligible(last_normal_publication_at=None)
            assert chosen is not None
            picks.append(chosen.offer_id)
        assert all(p == 99 for p in picks)

    def test_pick_random_eligible_skips_normal_under_cooldown(self):
        outbox = InMemoryOutbox(OutboxConfig(cooldown=CooldownPolicy(cooldown_seconds=300)))
        outbox.enqueue(_item(OutboxType.NORMAL.value, offer_id=1))
        outbox.enqueue(_item(OutboxType.NORMAL.value, offer_id=2))

        last = _now()
        # Justo después de publicar una normal, ninguna otra normal es elegible.
        chosen = outbox.pick_random_eligible(
            last_normal_publication_at=last,
            now=last + timedelta(seconds=10),
        )
        assert chosen is None

        # Pasados 5 min, sí.
        chosen = outbox.pick_random_eligible(
            last_normal_publication_at=last,
            now=last + timedelta(minutes=5, seconds=1),
        )
        assert chosen is not None
        assert chosen.type == OutboxType.NORMAL.value

    def test_mark_published_updates_state_and_attempts(self):
        outbox = InMemoryOutbox()
        item = outbox.enqueue(_item(OutboxType.NORMAL.value))
        updated = outbox.mark_published(item, now=_now())
        assert updated.state == OutboxState.SENT.value
        assert updated.attempts == 1
        assert updated.last_attempt_at == _now()

    def test_mark_discarded_records_reason(self):
        outbox = InMemoryOutbox()
        item = outbox.enqueue(_item(OutboxType.NORMAL.value))
        updated = outbox.mark_discarded(item, reason="no_image_after_revalidation")
        assert updated.state == OutboxState.DISCARDED.value
        assert updated.message_payload.get("discarded_reason") == "no_image_after_revalidation"
