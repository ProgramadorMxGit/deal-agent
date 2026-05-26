"""Cola persistente de publicación.

Implementa la API requerida por la spec:

- `enqueue(item)` — añade a la cola.
- `pick_random_eligible(now, last_normal_at)` — elige aleatoriamente entre
  los items elegibles (errores de precio inmediato, normales que cumplen
  cooldown, candidatos a revalidar). No es FIFO.
- `revalidate_age_threshold(now)` — devuelve los items con > `revalidate_after`
  de antigüedad que necesitan revalidación.
- `mark_published(item, evolution_response)`.
- `mark_failed(item, error)`.
- `mark_discarded(item, reason)`.

Esta versión es **in-memory + DB-backed**: persiste en SQLite cuando se le da
una conexión, pero también funciona sin DB para tests unitarios deterministas.
La capa async/serializada del dispatcher vive en `dispatcher.py` (Fase 3).
"""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator, Optional

from ..models import OutboxItem, OutboxState, OutboxType
from .cooldown import CooldownPolicy


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------


@dataclass
class OutboxConfig:
    revalidate_age_seconds: int = 3600  # 1 hora
    cooldown: CooldownPolicy = field(default_factory=CooldownPolicy)


# ---------------------------------------------------------------------------
# Outbox in-memory (suficiente para tests unitarios)
# ---------------------------------------------------------------------------


class InMemoryOutbox:
    """Cola en memoria con la misma API pública que la versión SQLite.

    Útil para tests unitarios y para uso temprano en desarrollo.
    """

    def __init__(self, config: Optional[OutboxConfig] = None) -> None:
        self.config = config or OutboxConfig()
        self._items: list[OutboxItem] = []
        self._next_id = 1
        self._rng = random.Random()

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

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
        """Items pendientes que pueden publicarse en este instante.

        Aplica cooldown a los `normal` y bypass a los `price_error` /
        `possible_pe`.
        """
        now = now or datetime.now(timezone.utc)
        eligible: list[OutboxItem] = []
        for item in self.pending():
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
        """Elige aleatoriamente uno de los elegibles.

        Si hay errores de precio, los **prioriza** sobre las ofertas normales
        antes de aleatorizar (la spec exige prioridad máxima para PE).
        """
        eligible = self.eligible_now(last_normal_publication_at, now)
        if not eligible:
            return None
        price_errors = [
            i for i in eligible if i.type in (OutboxType.PRICE_ERROR.value, OutboxType.POSSIBLE_PE.value)
        ]
        if price_errors:
            return self._rng.choice(price_errors)
        return self._rng.choice(eligible)

    def needs_revalidation(
        self, now: Optional[datetime] = None
    ) -> list[OutboxItem]:
        now = now or datetime.now(timezone.utc)
        threshold = timedelta(seconds=self.config.revalidate_age_seconds)
        return [
            i
            for i in self.pending()
            if (now - i.enqueued_at) > threshold
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

    def mark_failed(self, item: OutboxItem, now: Optional[datetime] = None) -> OutboxItem:
        now = now or datetime.now(timezone.utc)
        return self._update(
            item,
            state=OutboxState.FAILED.value,
            attempts=item.attempts + 1,
            last_attempt_at=now,
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
        """Reemplaza el payload tras una revalidación exitosa."""
        return self._update(item, message_payload=new_payload, enqueued_at=now or datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Outbox SQLite (Fase 2/3)
# ---------------------------------------------------------------------------


class SqliteOutbox(InMemoryOutbox):
    """Outbox respaldada por SQLite. Hereda la lógica de in-memory y se
    sincroniza con la DB.

    Es deliberadamente sencilla: cada operación traduce a INSERT/UPDATE.
    """

    def __init__(
        self, conn: sqlite3.Connection, config: Optional[OutboxConfig] = None
    ) -> None:
        super().__init__(config)
        self.conn = conn
        self._load_from_db()

    # ------------------------------------------------------------------
    # Persistencia
    # ------------------------------------------------------------------

    def _load_from_db(self) -> None:
        cur = self.conn.execute(
            "SELECT id, offer_id, type, enqueued_at, scheduled_for, attempts, "
            "       last_attempt_at, state, message_payload_json "
            "FROM outbox ORDER BY id ASC"
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
        self.conn.execute(
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
        )
        return item

    def _update(self, item: OutboxItem, **changes) -> OutboxItem:
        updated = super()._update(item, **changes)
        self.conn.execute(
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
        )
        return updated

    def pick_random_eligible(
        self,
        last_normal_publication_at: Optional[datetime],
        now: Optional[datetime] = None,
    ) -> Optional[OutboxItem]:
        """Versión atómica para SQLite: recarga desde DB y marca in_flight
        antes de devolver el item, evitando duplicados entre procesos.
        """
        now = now or datetime.now(timezone.utc)
        # Recargar desde DB para tener el estado más fresco
        self._load_from_db()
        item = super().pick_random_eligible(last_normal_publication_at, now)
        if item is None:
            return None
        # Marcar como in_flight atómicamente
        self.conn.execute(
            "UPDATE outbox SET state = ? WHERE id = ? AND state = ?",
            (OutboxState.IN_FLIGHT.value, item.id, OutboxState.PENDING.value),
        )
        self.conn.commit()
        # Verificar que realmente lo tomamos nosotros
        row = self.conn.execute(
            "SELECT state FROM outbox WHERE id = ?", (item.id,)
        ).fetchone()
        if row is None or (row["state"] if isinstance(row, dict) else row[0]) != OutboxState.IN_FLIGHT.value:
            return None  # Otro proceso lo tomó primero
        return item


# ---------------------------------------------------------------------------
# Helpers de datetime
# ---------------------------------------------------------------------------


def _fmt_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fmt_dt_optional(dt: Optional[datetime]) -> Optional[str]:
    return _fmt_dt(dt) if dt is not None else None


def _parse_dt(value: str) -> datetime:
    s = value.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def _parse_dt_optional(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return _parse_dt(value)


__all__ = [
    "InMemoryOutbox",
    "OutboxConfig",
    "SqliteOutbox",
]
