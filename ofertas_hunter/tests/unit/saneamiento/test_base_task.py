"""Tests unitarios para ``ofertas_hunter.saneamiento.tasks.base``.

Estos tests definen el contrato de la API descrito en el design.md del
spec ``bot-saneamiento-vps`` (sección ``Components and Interfaces`` →
``BaseSaneamientoTask`` y ``Helper _marketplace_filter_sql``). Se escriben
antes de la implementación (TDD-RED): la tarea 3.2 hará que estos tests
pasen.

Contrato resumido:

- ``marketplace_clause(marketplace) -> (sql_fragment, params)``:
    - ``"all"``           → ``("", ())``
    - ``"amazon"``        → ``(" AND LOWER(p.marketplace) = ? ", ("amazon",))``
    - ``"mercadolibre"``  → ``(" AND LOWER(p.marketplace) = ? ", ("mercadolibre",))``
- ``CandidateRow``: frozen dataclass con ``outbox_id``, ``marketplace``,
  ``title`` y ``extra`` (default ``{}``).
- ``BaseSaneamientoTask.apply(conn, candidates, *, clock)``:
    * ABC con ``name: ClassVar[str]`` y
      ``writes_last_attempt_at: ClassVar[bool] = True``.
    * Implementación default ejecuta
      ``UPDATE outbox SET state='discarded'[, last_attempt_at=:now]
      WHERE id=:id AND state='pending'``.
    * Devuelve el ``rowcount`` total mutado.
    * Respeta el guard ``state='pending'``: nunca toca filas
      ``discarded``/``sent``.
    * Con ``writes_last_attempt_at=False``, la columna
      ``last_attempt_at`` no se modifica (Requirement 4.5).

El esquema de ``outbox`` que se monta en :memory: es paridad estricta con
``migrations/001_init.sql`` (líneas 74-85), pero se omite el FK a
``offers`` porque ``apply`` sólo toca ``outbox``: la tarea 3.1 valida
únicamente la mutación del UPDATE compartido, no el SELECT/JOIN de
candidatos.
"""

from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from typing import Any

import pytest

from ofertas_hunter.saneamiento.tasks.base import (
    BaseSaneamientoTask,
    CandidateRow,
    marketplace_clause,
)


# ---------------------------------------------------------------------------
# Reloj determinista — paridad con las property tests del spec (clock fijo
# ``datetime(2026, 5, 29, 12, 0, 0, 123, tzinfo=timezone.utc)``).
# ---------------------------------------------------------------------------


_FIXED_DT = datetime(2026, 5, 29, 12, 0, 0, 123, tzinfo=timezone.utc)
_FIXED_ISO = "2026-05-29T12:00:00.000Z"
"""ISO-8601 con milisegundos y sufijo ``Z`` que el ``apply`` debe escribir
en ``last_attempt_at`` cuando ``writes_last_attempt_at=True``.

``microsecond=123`` se trunca a ``000`` ms al usar
``isoformat(timespec='milliseconds')`` (paridad con
``time_source.now_utc_iso``)."""


def fixed_clock() -> datetime:
    return _FIXED_DT


# ---------------------------------------------------------------------------
# Schema mínimo de ``outbox`` (paridad con migrations/001_init.sql)
# ---------------------------------------------------------------------------


_OUTBOX_SCHEMA = """
CREATE TABLE outbox (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id             INTEGER NOT NULL,
    type                 TEXT NOT NULL,
    enqueued_at          TEXT NOT NULL,
    scheduled_for        TEXT,
    attempts             INTEGER NOT NULL DEFAULT 0,
    last_attempt_at      TEXT,
    state                TEXT NOT NULL DEFAULT 'pending',
    message_payload_json TEXT NOT NULL
);
"""


def _make_conn() -> sqlite3.Connection:
    """Conexión :memory: con sólo la tabla ``outbox`` (paridad real)."""

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_OUTBOX_SCHEMA)
    return conn


def _insert_outbox_row(
    conn: sqlite3.Connection,
    *,
    state: str,
    last_attempt_at: str | None = None,
    enqueued_at: str = "2026-05-29T08:00:00.000Z",
    payload: str = '{"item_id": "X"}',
    type_: str = "normal",
    offer_id: int = 1,
) -> int:
    """Inserta una fila de ``outbox`` y devuelve su id."""

    cur = conn.execute(
        """
        INSERT INTO outbox (
            offer_id, type, enqueued_at, last_attempt_at,
            state, message_payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (offer_id, type_, enqueued_at, last_attempt_at, state, payload),
    )
    conn.commit()
    return int(cur.lastrowid)


def _fetch_row(conn: sqlite3.Connection, outbox_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, state, last_attempt_at FROM outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()
    assert row is not None, f"outbox row id={outbox_id} no encontrada"
    return row


# ---------------------------------------------------------------------------
# Subclases stub: contrato mínimo para ejercitar ``apply`` por defecto.
# ---------------------------------------------------------------------------


class _StubTaskWithLastAttempt(BaseSaneamientoTask):
    """Subclase mínima que activa ``writes_last_attempt_at=True``."""

    name = "stub-with-last-attempt"
    writes_last_attempt_at = True

    def select_candidates(  # pragma: no cover - no usado por estos tests
        self,
        conn: sqlite3.Connection,
        *,
        marketplace: str,
        clock: Any,
    ) -> list[CandidateRow]:
        return []


class _StubTaskWithoutLastAttempt(BaseSaneamientoTask):
    """Subclase mínima que desactiva ``writes_last_attempt_at`` (paridad con
    ``OutboxMissingPrevPriceTask``)."""

    name = "stub-without-last-attempt"
    writes_last_attempt_at = False

    def select_candidates(  # pragma: no cover - no usado por estos tests
        self,
        conn: sqlite3.Connection,
        *,
        marketplace: str,
        clock: Any,
    ) -> list[CandidateRow]:
        return []


def _candidate(outbox_id: int) -> CandidateRow:
    """Helper: ``CandidateRow`` mínimo para alimentar ``apply``."""

    return CandidateRow(
        outbox_id=outbox_id,
        marketplace="amazon",
        title="dummy",
    )


# ---------------------------------------------------------------------------
# A. ``marketplace_clause`` (Requirement 4.7)
# ---------------------------------------------------------------------------


class TestMarketplaceClause:
    """Cubre las tres ramas del helper SQL."""

    def test_all_returns_empty_fragment_and_params(self) -> None:
        assert marketplace_clause("all") == ("", ())

    def test_amazon_returns_lower_filter_and_amazon_param(self) -> None:
        assert marketplace_clause("amazon") == (
            " AND LOWER(p.marketplace) = ? ",
            ("amazon",),
        )

    def test_mercadolibre_returns_lower_filter_and_mercadolibre_param(
        self,
    ) -> None:
        assert marketplace_clause("mercadolibre") == (
            " AND LOWER(p.marketplace) = ? ",
            ("mercadolibre",),
        )


# ---------------------------------------------------------------------------
# D. ``CandidateRow`` — smoke test del dataclass frozen
# ---------------------------------------------------------------------------


class TestCandidateRow:
    """Cubre la forma del dataclass declarado en el design.

    El design lo especifica como ``@dataclass(frozen=True)`` con
    ``extra: dict[str, Any] = field(default_factory=dict)``.
    """

    def test_default_extra_is_empty_dict(self) -> None:
        row = CandidateRow(outbox_id=1, marketplace="amazon", title="t")

        assert row.outbox_id == 1
        assert row.marketplace == "amazon"
        assert row.title == "t"
        assert row.extra == {}

    def test_extra_can_be_supplied_explicitly(self) -> None:
        row = CandidateRow(
            outbox_id=2,
            marketplace=None,
            title=None,
            extra={"reason": "duplicate"},
        )

        assert row.extra == {"reason": "duplicate"}

    def test_is_frozen_assignment_raises(self) -> None:
        row = CandidateRow(outbox_id=3, marketplace="amazon", title="t")

        with pytest.raises(FrozenInstanceError):
            row.outbox_id = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# B. ``BaseSaneamientoTask.apply`` — comportamiento por defecto
# (Requirements 4.2, 4.3, 4.4, 4.5, 5.2, 5.3)
# ---------------------------------------------------------------------------


class TestApplyWithLastAttempt:
    """``writes_last_attempt_at=True`` (default): UPDATE con ``last_attempt_at``."""

    def test_only_pending_rows_are_mutated_and_rowcount_is_correct(
        self,
    ) -> None:
        """Inserta 3 filas (pending/discarded/sent), pasa los 3 ids como
        candidatos, verifica que sólo la pending muta y el rowcount==1."""

        conn = _make_conn()
        pending_id = _insert_outbox_row(conn, state="pending")
        discarded_id = _insert_outbox_row(
            conn,
            state="discarded",
            last_attempt_at="2025-01-01T00:00:00.000Z",
        )
        sent_id = _insert_outbox_row(
            conn,
            state="sent",
            last_attempt_at="2025-01-02T00:00:00.000Z",
        )

        task = _StubTaskWithLastAttempt()

        rowcount = task.apply(
            conn,
            [_candidate(pending_id), _candidate(discarded_id), _candidate(sent_id)],
            clock=fixed_clock,
        )

        # Sólo la fila pending fue mutada: rowcount debe ser 1.
        assert rowcount == 1

        pending_row = _fetch_row(conn, pending_id)
        discarded_row = _fetch_row(conn, discarded_id)
        sent_row = _fetch_row(conn, sent_id)

        # La pending pasó a discarded con last_attempt_at = now_iso del clock.
        assert pending_row["state"] == "discarded"
        assert pending_row["last_attempt_at"] == _FIXED_ISO

        # La discarded y la sent NO se tocaron: state y last_attempt_at intactos.
        assert discarded_row["state"] == "discarded"
        assert discarded_row["last_attempt_at"] == "2025-01-01T00:00:00.000Z"
        assert sent_row["state"] == "sent"
        assert sent_row["last_attempt_at"] == "2025-01-02T00:00:00.000Z"

    def test_multiple_pending_candidates_aggregate_rowcount(self) -> None:
        """Dos filas pending → rowcount==2 y ambas mutadas al mismo ``now_iso``."""

        conn = _make_conn()
        id_a = _insert_outbox_row(conn, state="pending")
        id_b = _insert_outbox_row(conn, state="pending")

        task = _StubTaskWithLastAttempt()

        rowcount = task.apply(
            conn,
            [_candidate(id_a), _candidate(id_b)],
            clock=fixed_clock,
        )

        assert rowcount == 2
        for outbox_id in (id_a, id_b):
            row = _fetch_row(conn, outbox_id)
            assert row["state"] == "discarded"
            assert row["last_attempt_at"] == _FIXED_ISO


class TestApplyWithoutLastAttempt:
    """``writes_last_attempt_at=False``: UPDATE sin ``last_attempt_at``
    (paridad con ``OutboxMissingPrevPriceTask``, Requirement 4.5)."""

    def test_pending_row_state_changes_but_last_attempt_at_unchanged(
        self,
    ) -> None:
        conn = _make_conn()
        # La fila se inserta SIN last_attempt_at (NULL) — tras el apply
        # debe seguir siendo NULL: el UPDATE no debe tocar esa columna.
        pending_id = _insert_outbox_row(conn, state="pending")

        task = _StubTaskWithoutLastAttempt()

        rowcount = task.apply(
            conn,
            [_candidate(pending_id)],
            clock=fixed_clock,
        )

        assert rowcount == 1
        row = _fetch_row(conn, pending_id)
        assert row["state"] == "discarded"
        assert row["last_attempt_at"] is None, (
            "writes_last_attempt_at=False ⇒ last_attempt_at no debe modificarse"
        )

    def test_existing_last_attempt_at_is_preserved_verbatim(self) -> None:
        """Si la fila ya tenía ``last_attempt_at``, el UPDATE no debe
        sobrescribirlo cuando ``writes_last_attempt_at=False``."""

        conn = _make_conn()
        original_lat = "2024-12-31T23:59:59.999Z"
        pending_id = _insert_outbox_row(
            conn,
            state="pending",
            last_attempt_at=original_lat,
        )

        task = _StubTaskWithoutLastAttempt()

        rowcount = task.apply(
            conn,
            [_candidate(pending_id)],
            clock=fixed_clock,
        )

        assert rowcount == 1
        row = _fetch_row(conn, pending_id)
        assert row["state"] == "discarded"
        assert row["last_attempt_at"] == original_lat


# ---------------------------------------------------------------------------
# C. ``apply`` con lista vacía — short-circuit no-op (Requirement 4.8)
# ---------------------------------------------------------------------------


class TestApplyEmptyCandidates:
    """Con ``candidates=[]`` el apply es no-op y devuelve 0."""

    def test_returns_zero_and_does_not_mutate_db(self) -> None:
        conn = _make_conn()
        pending_id = _insert_outbox_row(conn, state="pending")

        task = _StubTaskWithLastAttempt()

        rowcount = task.apply(conn, [], clock=fixed_clock)

        assert rowcount == 0

        # La fila pending sigue intacta.
        row = _fetch_row(conn, pending_id)
        assert row["state"] == "pending"
        assert row["last_attempt_at"] is None
