"""Persistent outbox queue."""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from ..db import execute_with_retry, run_with_retry
from ..models import OutboxItem, OutboxState, OutboxType
from .cooldown import CooldownPolicy


@dataclass
class OutboxConfig:
    revalidate_age_seconds: int = 3600
    cooldown: CooldownPolicy = field(default_factory=CooldownPolicy)


class InMemoryOutbox:
    """In-memory outbox with the same public API as the SQLite version."""

    def __init__(self, config: Optional[OutboxConfig] = None) -> None:
        self.config = config or OutboxConfig()
        self._items: list[OutboxItem] = []
        self._next_id = 1
        self._rng = random.Random()

    def enqueue(self, item: OutboxItem) -> OutboxItem:
        if item.id is None:
            item = replace(item, id=self._next_id)
            self._next_id += 1
        self._items.append(item)
        return item

    def pending(self) -> list[OutboxItem]:
        return [i for i in self._items if i.state == OutboxState.PENDING.value]

    def eligible_now(
        self,
        last_normal_publication_at: Optional[datetime],
        now: Optional[datetime] = None,
    ) -> list[OutboxItem]:
        now = now or datetime.now(timezone.utc)
        eligible: list[OutboxItem] = []
        for item in self.pending():
            if (item.message_payload or {}).get("requires_live_validation") is True:
                continue
            if item.scheduled_for and item.scheduled_for > now:
                continue
            if self.config.cooldown.is_publishable(
                item.type, last_normal_publication_at, now
            ):
                eligible.append(item)
        return eligible

    def pick_random_eligible(
        self,
        last_normal_publication_at: Optional[datetime],
        now: Optional[datetime] = None,
    ) -> Optional[OutboxItem]:
        eligible = self.eligible_now(last_normal_publication_at, now)
        if not eligible:
            return None
        price_errors = [
            i
            for i in eligible
            if i.type in (OutboxType.PRICE_ERROR.value, OutboxType.POSSIBLE_PE.value)
        ]
        if price_errors:
            return self._rng.choice(price_errors)
        return self._rng.choice(eligible)

    def needs_revalidation(self, now: Optional[datetime] = None) -> list[OutboxItem]:
        now = now or datetime.now(timezone.utc)
        threshold = timedelta(seconds=self.config.revalidate_age_seconds)
        return [
            i
            for i in self.pending()
            if (i.message_payload or {}).get("requires_live_validation") is True
            or (now - i.enqueued_at) > threshold
        ]

    def mark_published(
        self, item: OutboxItem, now: Optional[datetime] = None
    ) -> OutboxItem:
        now = now or datetime.now(timezone.utc)
        return self._update(
            item,
            state=OutboxState.SENT.value,
            attempts=item.attempts + 1,
            last_attempt_at=now,
        )

    def mark_failed(
        self, item: OutboxItem, now: Optional[datetime] = None
    ) -> OutboxItem:
        now = now or datetime.now(timezone.utc)
        return self._update(
            item,
            state=OutboxState.FAILED.value,
            attempts=item.attempts + 1,
            last_attempt_at=now,
        )

    def mark_retry_later(
        self,
        item: OutboxItem,
        *,
        now: Optional[datetime] = None,
        delay_seconds: int = 120,
        error: Optional[str] = None,
    ) -> OutboxItem:
        now = now or datetime.now(timezone.utc)
        new_payload = dict(item.message_payload)
        if error:
            new_payload["last_publish_error"] = error
        return self._update(
            item,
            state=OutboxState.PENDING.value,
            attempts=item.attempts + 1,
            last_attempt_at=now,
            scheduled_for=now + timedelta(seconds=max(1, int(delay_seconds))),
            message_payload=new_payload,
        )

    def mark_discarded(self, item: OutboxItem, reason: str) -> OutboxItem:
        new_payload = dict(item.message_payload)
        new_payload["discarded_reason"] = reason
        return self._update(
            item,
            state=OutboxState.DISCARDED.value,
            message_payload=new_payload,
        )

    def update_after_revalidation(
        self,
        item: OutboxItem,
        new_payload: dict,
        now: Optional[datetime] = None,
    ) -> OutboxItem:
        return self._update(
            item,
            message_payload=new_payload,
            enqueued_at=now or datetime.now(timezone.utc),
        )

    def _update(self, item: OutboxItem, **changes) -> OutboxItem:
        for idx, existing in enumerate(self._items):
            if existing.id == item.id:
                updated = replace(existing, **changes)
                self._items[idx] = updated
                return updated
        raise KeyError(f"OutboxItem id={item.id} no encontrado")

    def __iter__(self) -> Iterator[OutboxItem]:
        return iter(list(self._items))

    def __len__(self) -> int:
        return len(self._items)


class SqliteOutbox(InMemoryOutbox):
    """SQLite-backed outbox with short transactions and retry on lock."""

    def __init__(
        self, conn: sqlite3.Connection, config: Optional[OutboxConfig] = None
    ) -> None:
        super().__init__(config)
        self.conn = conn
        self._load_from_db()

    def _load_from_db(self) -> None:
        cur = execute_with_retry(
            self.conn,
            "SELECT id, offer_id, type, enqueued_at, scheduled_for, attempts, "
            "       last_attempt_at, state, message_payload_json "
            "FROM outbox ORDER BY id ASC",
            description="outbox load",
        )
        self._items = []
        max_id = 0
        for row in cur.fetchall():
            self._items.append(
                OutboxItem(
                    id=row["id"],
                    offer_id=row["offer_id"],
                    type=row["type"],
                    enqueued_at=_parse_dt(row["enqueued_at"]),
                    scheduled_for=_parse_dt_optional(row["scheduled_for"]),
                    attempts=row["attempts"],
                    last_attempt_at=_parse_dt_optional(row["last_attempt_at"]),
                    state=row["state"],
                    message_payload=json.loads(row["message_payload_json"]),
                )
            )
            max_id = max(max_id, row["id"])
        self._next_id = max_id + 1

    def enqueue(self, item: OutboxItem) -> OutboxItem:
        item = super().enqueue(item)
        execute_with_retry(
            self.conn,
            "INSERT INTO outbox(id, offer_id, type, enqueued_at, scheduled_for, "
            "attempts, last_attempt_at, state, message_payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.id,
                item.offer_id,
                item.type,
                _fmt_dt(item.enqueued_at),
                _fmt_dt_optional(item.scheduled_for),
                item.attempts,
                _fmt_dt_optional(item.last_attempt_at),
                item.state,
                json.dumps(item.message_payload, ensure_ascii=False),
            ),
            description=f"outbox enqueue id={item.id}",
        )
        return item

    def _update(self, item: OutboxItem, **changes) -> OutboxItem:
        updated = super()._update(item, **changes)
        execute_with_retry(
            self.conn,
            "UPDATE outbox SET offer_id=?, type=?, enqueued_at=?, scheduled_for=?, "
            "attempts=?, last_attempt_at=?, state=?, message_payload_json=? "
            "WHERE id=?",
            (
                updated.offer_id,
                updated.type,
                _fmt_dt(updated.enqueued_at),
                _fmt_dt_optional(updated.scheduled_for),
                updated.attempts,
                _fmt_dt_optional(updated.last_attempt_at),
                updated.state,
                json.dumps(updated.message_payload, ensure_ascii=False),
                updated.id,
            ),
            description=f"outbox update id={updated.id}",
        )
        return updated

    def pick_random_eligible(
        self,
        last_normal_publication_at: Optional[datetime],
        now: Optional[datetime] = None,
    ) -> Optional[OutboxItem]:
        now = now or datetime.now(timezone.utc)

        def _claim_once() -> Optional[OutboxItem]:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self._load_from_db()
                item = super(SqliteOutbox, self).pick_random_eligible(
                    last_normal_publication_at,
                    now,
                )
                if item is None:
                    self.conn.commit()
                    return None

                cur = self.conn.execute(
                    "UPDATE outbox SET state = ? WHERE id = ? AND state = ?",
                    (OutboxState.IN_FLIGHT.value, item.id, OutboxState.PENDING.value),
                )
                if cur.rowcount != 1:
                    self.conn.commit()
                    return None

                self.conn.commit()
                return InMemoryOutbox._update(
                    self,
                    item,
                    state=OutboxState.IN_FLIGHT.value,
                )
            except Exception:
                try:
                    self.conn.rollback()
                except sqlite3.Error:
                    pass
                raise

        return run_with_retry(
            _claim_once,
            description="outbox claim item",
        )

    def eligible_now(
        self,
        last_normal_publication_at: Optional[datetime],
        now: Optional[datetime] = None,
    ) -> list[OutboxItem]:
        return super().eligible_now(last_normal_publication_at, now)

    def needs_revalidation(self, now: Optional[datetime] = None) -> list[OutboxItem]:
        self._load_from_db()
        return super().needs_revalidation(now=now)


def _fmt_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        dt.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _fmt_dt_optional(dt: Optional[datetime]) -> Optional[str]:
    return _fmt_dt(dt) if dt is not None else None


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_dt_optional(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return _parse_dt(value)


__all__ = [
    "InMemoryOutbox",
    "OutboxConfig",
    "SqliteOutbox",
]
