"""Tests unitarios para ``ofertas_hunter.saneamiento.tasks.outbox_stale``.

Estos tests definen el contrato de la ``Saneamiento_Task``
``outbox-stale`` descrito en
``.kiro/specs/bot-saneamiento-vps/design.md`` (sección
``Components and Interfaces`` → ``OutboxStaleTask``). Se escriben
antes de la implementación (TDD-RED): la tarea 7.2 hará que estos
tests pasen.

Paridad estricta con ``scripts/_vps_cleanup_stale.py``:

- SELECT con join ``outbox o JOIN offers of JOIN products p`` y
  filtros::

      WHERE o.state = 'pending'
        AND o.type  = 'normal'
        AND o.enqueued_at < :cutoff_4h
        AND json_extract(o.message_payload_json, '$.previous_price') IS NOT NULL
        <MKT>

- Cutoff: ``:cutoff_4h = cutoff_iso(clock, 4)`` (estrictamente menor:
  una fila con ``enqueued_at`` exactamente igual al cutoff NO es
  candidato).
- ``writes_last_attempt_at = True``: el ``apply`` heredado actualiza
  ``last_attempt_at`` con el ``now_iso`` del clock inyectado
  (Requirement 4.2, 4.8).

Cobertura (Requirement 8.5):

1. Caso vacío: DB sin filas → ``inspected=0`` y ``affected=0``.
2. Caso Dry_Run con candidatos: snapshot byte-idéntico antes/después
   (Requirements 5.1, 5.2, 5.3, 5.4).
3. Caso Apply_Mode con candidatos: ``state='discarded'`` y
   ``last_attempt_at = FIXED_ISO`` (Requirements 4.2, 4.8).
4. Caso idempotencia: segunda ``Saneamiento_Run`` reporta ``affected=0``
   (Requirement 5.1).
5. Caso ``--marketplace=mercadolibre``: outbox de ``amazon`` no se
   incluye como candidato (Requirement 4.7).
6. Caso límite del cutoff de 4h:
   - ``enqueued_at`` exactamente igual a ``cutoff_4h`` → NO candidato
     (la SQL usa ``<`` estricto).
   - ``enqueued_at`` 5 min antes (más antiguo) → candidato.
   - ``enqueued_at`` 5 min después (más reciente) → NO candidato.
7. Caso ``type != 'normal'`` (p. ej. ``price_error``): NO candidato.
8. Caso ``previous_price IS NULL`` o ausente del payload JSON:
   NO candidato.
9. Caso state guard: ``discarded``/``sent`` no aparecen como candidatos.

Reloj fijo (paridad con tests de las otras tasks del spec):

    FIXED_DT  = datetime(2026, 5, 29, 12, 0, 0, 0, tzinfo=timezone.utc)
    FIXED_ISO = "2026-05-29T12:00:00.000Z"

Por construcción, ``cutoff_iso(fixed_clock, 4) == "2026-05-29T08:00:00.000Z"``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from ofertas_hunter.saneamiento.tasks.outbox_stale import OutboxStaleTask


# ---------------------------------------------------------------------------
# Reloj determinista — paridad con las property tests del spec.
# ---------------------------------------------------------------------------


FIXED_DT = datetime(2026, 5, 29, 12, 0, 0, 0, tzinfo=timezone.utc)
FIXED_ISO = "2026-05-29T12:00:00.000Z"
"""ISO-8601 ms con sufijo ``Z`` que ``apply`` debe escribir en
``last_attempt_at`` cuando muta una fila stale."""

CUTOFF_4H_ISO = "2026-05-29T08:00:00.000Z"
"""``cutoff_iso(fixed_clock, 4)`` precomputado.

La SQL filtra con ``o.enqueued_at < :cutoff_4h`` (estrictamente
menor), así que una fila con ``enqueued_at == CUTOFF_4H_ISO`` NO
es candidato.
"""

# 5 minutos antes y después del cutoff, formateados igual que el
# resto del bot (ms y sufijo ``Z``).
ENQUEUED_FIVE_MIN_BEFORE_CUTOFF = "2026-05-29T07:55:00.000Z"
ENQUEUED_FIVE_MIN_AFTER_CUTOFF = "2026-05-29T08:05:00.000Z"

# Un timestamp claramente "viejo" (anterior al cutoff por mucho)
# útil para tests donde la antigüedad no es la variable bajo prueba.
OLD_ENQUEUED_AT = "2026-05-29T00:00:00.000Z"


def fixed_clock() -> datetime:
    return FIXED_DT


# ---------------------------------------------------------------------------
# Schema mínimo (paridad con migrations/001_init.sql)
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE products (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace          TEXT NOT NULL,
    marketplace_id       TEXT,
    url_canonical        TEXT NOT NULL,
    title                TEXT NOT NULL,
    brand                TEXT,
    category             TEXT,
    category_inferred    INTEGER NOT NULL DEFAULT 0,
    condition            TEXT NOT NULL DEFAULT 'new',
    image_url            TEXT,
    affiliate_link       TEXT,
    affiliate_product_id TEXT,
    commission_text      TEXT,
    first_seen_at        TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL
);

CREATE TABLE offers (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id                    INTEGER NOT NULL,
    current_price_observation_id  INTEGER,
    classification                TEXT NOT NULL,
    score                         INTEGER NOT NULL DEFAULT 0,
    reasons_json                  TEXT NOT NULL DEFAULT '[]',
    discount_percent              REAL,
    state                         TEXT NOT NULL,
    created_at                    TEXT NOT NULL,
    updated_at                    TEXT NOT NULL
);

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
    """Conexión :memory: con el schema mínimo paridad real."""

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Helpers de inserción
# ---------------------------------------------------------------------------


def _insert_product(
    conn: sqlite3.Connection,
    *,
    marketplace: str = "amazon",
    title: str = "Producto X",
    url_canonical: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO products (
            marketplace, url_canonical, title,
            first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            marketplace,
            url_canonical or f"https://example.com/{marketplace}/{title}",
            title,
            "2026-05-25T00:00:00.000Z",
            "2026-05-29T00:00:00.000Z",
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_offer(
    conn: sqlite3.Connection,
    *,
    product_id: int,
    classification: str = "normal",
    state: str = "new",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO offers (
            product_id, classification, score, reasons_json,
            state, created_at, updated_at
        ) VALUES (?, ?, 0, '[]', ?, ?, ?)
        """,
        (
            product_id,
            classification,
            state,
            "2026-05-29T00:00:00.000Z",
            "2026-05-29T00:00:00.000Z",
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_outbox(
    conn: sqlite3.Connection,
    *,
    offer_id: int,
    payload: dict[str, Any],
    state: str = "pending",
    type_: str = "normal",
    enqueued_at: str = OLD_ENQUEUED_AT,
    last_attempt_at: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO outbox (
            offer_id, type, enqueued_at, last_attempt_at,
            state, message_payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            offer_id,
            type_,
            enqueued_at,
            last_attempt_at,
            state,
            json.dumps(payload),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _stale_payload(
    *,
    title: str = "Producto",
    previous_price: Any = 99.99,
) -> dict[str, Any]:
    """Payload con ``previous_price`` no nulo (candidato si es stale).

    El SELECT del task filtra por
    ``json_extract(payload, '$.previous_price') IS NOT NULL``, por lo
    que cualquier valor distinto de ``None`` (incluso ``0``) hace
    candidata a la fila siempre que cumpla el resto de filtros.
    """

    return {"title": title, "previous_price": previous_price}


# ---------------------------------------------------------------------------
# Snapshots — para verificar que Dry_Run no muta la DB.
# ---------------------------------------------------------------------------


_TABLES_TO_SNAPSHOT = ("products", "offers", "outbox")


def _take_snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    """Snapshot fila-a-fila ordenado por id de las tablas relevantes."""

    snap: dict[str, list[tuple[Any, ...]]] = {}
    for table in _TABLES_TO_SNAPSHOT:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        snap[table] = [tuple(row) for row in rows]
    return snap


def _fetch_outbox(conn: sqlite3.Connection, outbox_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, state, last_attempt_at FROM outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()
    assert row is not None, f"outbox row id={outbox_id} no encontrada"
    return row


def _select_ids(candidates: Iterable[Any]) -> list[int]:
    return sorted(c.outbox_id for c in candidates)


# ---------------------------------------------------------------------------
# 1) Caso vacío
# ---------------------------------------------------------------------------


class TestEmptyDatabase:
    """DB sin filas → no hay candidatos ni mutaciones."""

    def test_select_candidates_returns_empty_list(self) -> None:
        conn = _make_conn()
        task = OutboxStaleTask()

        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_apply_with_empty_list_returns_zero(self) -> None:
        conn = _make_conn()
        task = OutboxStaleTask()

        affected = task.apply(conn, [], clock=fixed_clock)

        assert affected == 0


# ---------------------------------------------------------------------------
# 2) Caso Dry_Run con candidatos: snapshot byte-idéntico antes/después.
# ---------------------------------------------------------------------------


class TestDryRunWithCandidates:
    """``select_candidates`` por sí solo NO debe mutar la DB.

    Validates: Requirements 5.1, 5.2, 5.3, 5.4
    """

    def test_select_candidates_does_not_mutate_db(self) -> None:
        conn = _make_conn()

        product_id = _insert_product(
            conn, marketplace="amazon", title="Auriculares"
        )
        offer_id = _insert_offer(conn, product_id=product_id)

        # ``enqueued_at`` mucho más antiguo que el cutoff y payload con
        # ``previous_price`` ⇒ candidato típico.
        candidate_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_stale_payload(title="Auriculares"),
            enqueued_at=OLD_ENQUEUED_AT,
        )

        snapshot_before = _take_snapshot(conn)

        task = OutboxStaleTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [candidate_outbox]

        # La DB no se tocó por la sola selección (Dry_Run).
        snapshot_after = _take_snapshot(conn)
        assert snapshot_before == snapshot_after


# ---------------------------------------------------------------------------
# 3) Caso Apply_Mode: discarded + last_attempt_at actualizado.
# ---------------------------------------------------------------------------


class TestApplyModeMarksDiscardedWithLastAttemptAt:
    """``apply`` muta ``state='discarded'`` Y ``last_attempt_at = FIXED_ISO``.

    Validates: Requirements 4.2, 4.8
    """

    def test_candidate_becomes_discarded_with_last_attempt_at(self) -> None:
        conn = _make_conn()

        product_id = _insert_product(
            conn, marketplace="amazon", title="Cargador"
        )
        offer_id = _insert_offer(conn, product_id=product_id)

        candidate_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_stale_payload(title="Cargador"),
            enqueued_at=OLD_ENQUEUED_AT,
        )

        task = OutboxStaleTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )
        affected = task.apply(conn, candidates, clock=fixed_clock)

        assert _select_ids(candidates) == [candidate_outbox]
        assert affected == 1

        row = _fetch_outbox(conn, candidate_outbox)
        assert row["state"] == "discarded"
        assert row["last_attempt_at"] == FIXED_ISO

    def test_non_candidate_row_is_not_mutated(self) -> None:
        """Sanity-check: una fila reciente (después del cutoff) queda intacta."""

        conn = _make_conn()

        product_id = _insert_product(conn, marketplace="amazon", title="OK")
        offer_id = _insert_offer(conn, product_id=product_id)

        # ``enqueued_at`` después del cutoff ⇒ NO stale ⇒ NO candidato.
        recent_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_stale_payload(title="OK"),
            enqueued_at=ENQUEUED_FIVE_MIN_AFTER_CUTOFF,
        )

        task = OutboxStaleTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )
        affected = task.apply(conn, candidates, clock=fixed_clock)

        assert candidates == []
        assert affected == 0

        row = _fetch_outbox(conn, recent_outbox)
        assert row["state"] == "pending"
        assert row["last_attempt_at"] is None


# ---------------------------------------------------------------------------
# 4) Caso idempotencia: segunda ejecución → affected=0.
# ---------------------------------------------------------------------------


class TestIdempotenceSecondRunZeroAffected:
    """Una segunda Saneamiento_Run consecutiva no afecta nada.

    Validates: Requirement 5.1
    """

    def test_second_run_reports_zero_candidates_and_zero_affected(self) -> None:
        conn = _make_conn()

        product_id = _insert_product(conn, marketplace="amazon", title="Mouse")
        offer_id = _insert_offer(conn, product_id=product_id)

        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_stale_payload(title="Mouse"),
            enqueued_at=OLD_ENQUEUED_AT,
        )

        task = OutboxStaleTask()

        # Primera run: selecciona y aplica.
        first_candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )
        first_affected = task.apply(conn, first_candidates, clock=fixed_clock)
        assert first_affected == 1
        assert _select_ids(first_candidates) == [outbox_id]

        # Segunda run: la fila ya está ``discarded``, no debe aparecer.
        second_candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )
        second_affected = task.apply(conn, second_candidates, clock=fixed_clock)

        assert second_candidates == []
        assert second_affected == 0


# ---------------------------------------------------------------------------
# 5) Caso filtro --marketplace=mercadolibre: outbox de amazon no se afecta.
# ---------------------------------------------------------------------------


class TestMarketplaceFilterMercadolibreExcludesAmazon:
    """Con ``--marketplace=mercadolibre`` los outbox de ``amazon`` se excluyen.

    Validates: Requirement 4.7
    """

    def test_amazon_outbox_is_not_in_mercadolibre_candidates(self) -> None:
        conn = _make_conn()

        # Lado mercadolibre: candidato (stale + prev_price presente).
        ml_product = _insert_product(
            conn, marketplace="mercadolibre", title="ML Producto"
        )
        ml_offer = _insert_offer(conn, product_id=ml_product)
        ml_outbox = _insert_outbox(
            conn,
            offer_id=ml_offer,
            payload=_stale_payload(title="ML"),
            enqueued_at=OLD_ENQUEUED_AT,
        )

        # Lado amazon: también candidato a nivel temporal/payload, pero
        # con filtro ``mercadolibre`` debe quedar fuera.
        amazon_product = _insert_product(
            conn, marketplace="amazon", title="Amazon Producto"
        )
        amazon_offer = _insert_offer(conn, product_id=amazon_product)
        amazon_outbox = _insert_outbox(
            conn,
            offer_id=amazon_offer,
            payload=_stale_payload(title="Amazon"),
            enqueued_at=OLD_ENQUEUED_AT,
        )

        task = OutboxStaleTask()

        # marketplace=mercadolibre → solo el outbox de mercadolibre.
        ml_candidates = task.select_candidates(
            conn, marketplace="mercadolibre", clock=fixed_clock
        )
        assert _select_ids(ml_candidates) == [ml_outbox]

        affected = task.apply(conn, ml_candidates, clock=fixed_clock)
        assert affected == 1

        ml_row = _fetch_outbox(conn, ml_outbox)
        assert ml_row["state"] == "discarded"
        assert ml_row["last_attempt_at"] == FIXED_ISO

        # El outbox de amazon no se tocó.
        amazon_row = _fetch_outbox(conn, amazon_outbox)
        assert amazon_row["state"] == "pending"
        assert amazon_row["last_attempt_at"] is None

    def test_marketplace_all_includes_both_markets(self) -> None:
        """Sanity-check: ``marketplace='all'`` sí incluye ambos lados."""

        conn = _make_conn()

        ml_product = _insert_product(
            conn, marketplace="mercadolibre", title="A"
        )
        ml_offer = _insert_offer(conn, product_id=ml_product)
        ml_outbox = _insert_outbox(
            conn,
            offer_id=ml_offer,
            payload=_stale_payload(title="A"),
            enqueued_at=OLD_ENQUEUED_AT,
        )

        amazon_product = _insert_product(
            conn, marketplace="amazon", title="B"
        )
        amazon_offer = _insert_offer(conn, product_id=amazon_product)
        amazon_outbox = _insert_outbox(
            conn,
            offer_id=amazon_offer,
            payload=_stale_payload(title="B"),
            enqueued_at=OLD_ENQUEUED_AT,
        )

        task = OutboxStaleTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == sorted([ml_outbox, amazon_outbox])


# ---------------------------------------------------------------------------
# 6) Caso límite del cutoff de 4h: estrictamente menor (``<``).
# ---------------------------------------------------------------------------


class TestCutoff4HoursBoundary:
    """Paridad con ``_vps_cleanup_stale.py``: ``enqueued_at < cutoff_4h``.

    Para un clock fijo en ``2026-05-29T12:00:00.000Z``,
    ``cutoff_4h == 2026-05-29T08:00:00.000Z``. La SQL usa ``<``
    estricto, así que:

    - ``enqueued_at == cutoff_4h``               → NO candidato (igualdad).
    - ``enqueued_at == cutoff_4h - 5min``        → candidato (más viejo).
    - ``enqueued_at == cutoff_4h + 5min``        → NO candidato (más nuevo).
    """

    def test_enqueued_exactly_at_cutoff_is_not_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="At")
        offer_id = _insert_offer(conn, product_id=product_id)

        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_stale_payload(title="At"),
            enqueued_at=CUTOFF_4H_ISO,
        )

        task = OutboxStaleTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_enqueued_five_minutes_before_cutoff_is_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="Before")
        offer_id = _insert_offer(conn, product_id=product_id)

        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_stale_payload(title="Before"),
            enqueued_at=ENQUEUED_FIVE_MIN_BEFORE_CUTOFF,
        )

        task = OutboxStaleTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]

    def test_enqueued_five_minutes_after_cutoff_is_not_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="After")
        offer_id = _insert_offer(conn, product_id=product_id)

        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_stale_payload(title="After"),
            enqueued_at=ENQUEUED_FIVE_MIN_AFTER_CUTOFF,
        )

        task = OutboxStaleTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []


# ---------------------------------------------------------------------------
# 7) Caso ``type != 'normal'``: NO candidato (paridad con script).
# ---------------------------------------------------------------------------


class TestTypeMustBeNormal:
    """El script legacy filtra ``AND o.type='normal'``.

    Una fila con ``type != 'normal'`` (p. ej. ``price_error``) nunca
    debe aparecer como candidato, aun cuando el resto de filtros
    (state, enqueued_at, payload) la satisfagan.
    """

    def test_type_price_error_is_not_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="PE")
        offer_id = _insert_offer(conn, product_id=product_id)

        # Stale + prev_price + state pending pero ``type='price_error'``.
        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_stale_payload(title="PE"),
            enqueued_at=OLD_ENQUEUED_AT,
            type_="price_error",
        )

        task = OutboxStaleTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_type_normal_is_candidate(self) -> None:
        """Sanity-check: ``type='normal'`` con el resto de filtros válidos
        sí dispara como candidato (control positivo del test anterior)."""

        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="N")
        offer_id = _insert_offer(conn, product_id=product_id)

        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_stale_payload(title="N"),
            enqueued_at=OLD_ENQUEUED_AT,
            type_="normal",
        )

        task = OutboxStaleTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]


# ---------------------------------------------------------------------------
# 8) Caso ``previous_price IS NULL`` o ausente del payload: NO candidato.
# ---------------------------------------------------------------------------


class TestPayloadMustHavePreviousPrice:
    """El script legacy filtra
    ``json_extract(payload, '$.previous_price') IS NOT NULL``.

    Cubre:
    - ``previous_price`` explícito como ``None`` (JSON ``null``).
    - Clave ``previous_price`` ausente del payload (``json_extract``
      devuelve NULL ⇒ falla el filtro).
    - ``previous_price`` con valor numérico válido ⇒ candidato (control
      positivo).
    """

    def test_explicit_null_previous_price_is_not_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="Pn")
        offer_id = _insert_offer(conn, product_id=product_id)

        # Payload con previous_price=None ⇒ JSON ``null`` ⇒
        # ``json_extract`` retorna NULL ⇒ NO candidato.
        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"title": "Pn", "previous_price": None},
            enqueued_at=OLD_ENQUEUED_AT,
        )

        task = OutboxStaleTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_missing_previous_price_key_is_not_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="Pm")
        offer_id = _insert_offer(conn, product_id=product_id)

        # Payload sin la clave ``previous_price`` ⇒ ``json_extract``
        # devuelve NULL ⇒ NO candidato.
        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"title": "Pm", "current_price": 50.0},
            enqueued_at=OLD_ENQUEUED_AT,
        )

        task = OutboxStaleTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_previous_price_set_is_candidate(self) -> None:
        """Control positivo: ``previous_price=10.0`` sí dispara candidato."""

        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="Pv")
        offer_id = _insert_offer(conn, product_id=product_id)

        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"title": "Pv", "previous_price": 10.0},
            enqueued_at=OLD_ENQUEUED_AT,
        )

        task = OutboxStaleTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]


# ---------------------------------------------------------------------------
# 9) State guard: discarded/sent NO aparecen como candidatos.
# ---------------------------------------------------------------------------


class TestStateGuardOnlyPending:
    """El SELECT exige ``o.state='pending'``.

    Validates: Requirement 5.2 (no-borrado) y paridad con el script.
    """

    def test_discarded_and_sent_rows_are_not_candidates(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="SG")
        offer_id = _insert_offer(conn, product_id=product_id)

        # Pre-existente discarded: no debe aparecer.
        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_stale_payload(title="D"),
            enqueued_at=OLD_ENQUEUED_AT,
            state="discarded",
            last_attempt_at="2025-01-01T00:00:00.000Z",
        )

        # Pre-existente sent: tampoco debe aparecer.
        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_stale_payload(title="S"),
            enqueued_at=OLD_ENQUEUED_AT,
            state="sent",
            last_attempt_at="2025-01-02T00:00:00.000Z",
        )

        task = OutboxStaleTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []
