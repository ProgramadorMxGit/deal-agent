"""Tests unitarios para ``ofertas_hunter.saneamiento.tasks.outbox_missing_prev_price``.

Estos tests definen el contrato de la ``Saneamiento_Task``
``outbox-missing-prev-price`` descrito en
``.kiro/specs/bot-saneamiento-vps/design.md`` (sección
``Components and Interfaces`` → ``OutboxMissingPrevPriceTask``).
Se escriben antes de la implementación (TDD-RED): la tarea 5.2
hará que estos tests pasen.

Paridad estricta con ``scripts/cleanup_outbox_missing_prev_price.py``:

- SELECT con join ``outbox o JOIN offers of JOIN products p`` y
  filtro ``o.state='pending'`` + ``marketplace_clause``.
- Filtrado en Python por ``LOWER(classification) == 'price_error_confirmed'``
  (case-insensitive: la SELECT ya devuelve ``LOWER(of.classification)``).
- Filtrado por **truthiness** del payload: ``payload.get('previous_price')``
  y ``payload.get('discount_percent')`` se evalúan con ``not prev or not disc``,
  por lo que ``None``, ``0``, ``0.0`` y ``""`` cuentan como faltantes
  (paridad literal con el script).
- ``writes_last_attempt_at=False``: el ``apply`` heredado NO actualiza
  ``last_attempt_at`` (Requirement 4.5).

Cobertura (Requirement 8.5):

1. Caso vacío.
2. Caso Dry_Run con candidatos: snapshot byte-idéntico antes/después.
3. Caso Apply_Mode con candidatos: ``state='discarded'`` y
   ``last_attempt_at`` SIN modificar (paridad legacy).
4. Caso idempotencia.
5. Caso ``--marketplace=mercadolibre``.
6. Caso exclusión ``classification='price_error_confirmed'`` case-insensitive.
7. Caso truthiness del payload (``None`` / ``0`` / ``""`` ⇒ candidato).
8. Caso row con ``previous_price`` y ``discount_percent`` truthy ⇒ NO candidato.
9. Caso state guard: ``discarded``/``sent`` no aparecen como candidatos.

Reloj fijo (paridad con tests de ``outbox-duplicates``):

    FIXED_DT = datetime(2026, 5, 29, 12, 0, 0, 0, tzinfo=timezone.utc)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from ofertas_hunter.saneamiento.tasks.outbox_missing_prev_price import (
    OutboxMissingPrevPriceTask,
)


# ---------------------------------------------------------------------------
# Reloj determinista — paridad con las property tests del spec.
# ---------------------------------------------------------------------------


FIXED_DT = datetime(2026, 5, 29, 12, 0, 0, 0, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    return FIXED_DT


# Valor fijo de ``last_attempt_at`` pre-existente que se usa para
# verificar que ``apply`` NO lo modifica (paridad con script legacy).
PREEXISTING_LAST_ATTEMPT_AT = "2025-01-01T00:00:00.000Z"


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
    enqueued_at: str = "2026-05-29T11:00:00.000Z",
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


# A payload that is a "valid" entry (truthy prev + disc) — used as the
# control case where the row should NOT be a candidate.
def _valid_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Producto válido",
        "current_price": 8.0,
        "previous_price": 10.0,
        "discount_percent": 20.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1) Caso vacío
# ---------------------------------------------------------------------------


class TestEmptyDatabase:
    """DB sin filas → no hay candidatos ni mutaciones."""

    def test_select_candidates_returns_empty_list(self) -> None:
        conn = _make_conn()
        task = OutboxMissingPrevPriceTask()

        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_apply_with_empty_list_returns_zero(self) -> None:
        conn = _make_conn()
        task = OutboxMissingPrevPriceTask()

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
        offer_id = _insert_offer(
            conn, product_id=product_id, classification="normal"
        )

        # Outbox pending con payload sin previous_price → candidato.
        candidate_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={
                "title": "Auriculares",
                "current_price": 50.0,
                # sin previous_price ni discount_percent
            },
        )

        snapshot_before = _take_snapshot(conn)

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [candidate_outbox]

        # La DB no se tocó por la sola selección (Dry_Run).
        snapshot_after = _take_snapshot(conn)
        assert snapshot_before == snapshot_after


# ---------------------------------------------------------------------------
# 3) Caso Apply_Mode: discarded SIN tocar last_attempt_at (Req 4.5).
# ---------------------------------------------------------------------------


class TestApplyDoesNotTouchLastAttemptAt:
    """``apply`` muta ``state='discarded'`` pero NO ``last_attempt_at``.

    Paridad con ``scripts/cleanup_outbox_missing_prev_price.py`` que
    sólo ejecuta ``UPDATE outbox SET state='discarded' WHERE id=?``.

    Validates: Requirement 4.5
    """

    def test_existing_last_attempt_at_is_preserved_after_apply(self) -> None:
        conn = _make_conn()

        product_id = _insert_product(
            conn, marketplace="amazon", title="Cargador"
        )
        offer_id = _insert_offer(
            conn, product_id=product_id, classification="normal"
        )

        candidate_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"title": "Cargador", "current_price": 30.0},
            last_attempt_at=PREEXISTING_LAST_ATTEMPT_AT,
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )
        affected = task.apply(conn, candidates, clock=fixed_clock)

        assert affected == 1

        row = _fetch_outbox(conn, candidate_outbox)
        assert row["state"] == "discarded"
        # CRÍTICO: last_attempt_at queda exactamente como antes (no se
        # actualizó al ``now_iso`` del clock).
        assert row["last_attempt_at"] == PREEXISTING_LAST_ATTEMPT_AT

    def test_null_last_attempt_at_remains_null_after_apply(self) -> None:
        conn = _make_conn()

        product_id = _insert_product(
            conn, marketplace="amazon", title="Mouse"
        )
        offer_id = _insert_offer(
            conn, product_id=product_id, classification="normal"
        )

        candidate_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"title": "Mouse", "current_price": 20.0},
            last_attempt_at=None,
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )
        affected = task.apply(conn, candidates, clock=fixed_clock)

        assert affected == 1

        row = _fetch_outbox(conn, candidate_outbox)
        assert row["state"] == "discarded"
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

        product_id = _insert_product(
            conn, marketplace="amazon", title="Teclado"
        )
        offer_id = _insert_offer(
            conn, product_id=product_id, classification="normal"
        )

        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"title": "Teclado", "current_price": 40.0},
            last_attempt_at=PREEXISTING_LAST_ATTEMPT_AT,
        )

        task = OutboxMissingPrevPriceTask()

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

        # last_attempt_at sigue intacto incluso tras dos runs.
        row = _fetch_outbox(conn, outbox_id)
        assert row["state"] == "discarded"
        assert row["last_attempt_at"] == PREEXISTING_LAST_ATTEMPT_AT


# ---------------------------------------------------------------------------
# 5) Caso filtro --marketplace=mercadolibre: outbox de amazon no se afecta.
# ---------------------------------------------------------------------------


class TestMarketplaceFilterMercadolibreExcludesAmazon:
    """Con ``--marketplace=mercadolibre`` los outbox de ``amazon`` se excluyen.

    Validates: Requirement 4.7
    """

    def test_amazon_outbox_is_not_in_mercadolibre_candidates(self) -> None:
        conn = _make_conn()

        # Lado amazon: payload sin prev_price → sería candidato si no hubiera filtro.
        amazon_product = _insert_product(
            conn, marketplace="amazon", title="Amazon Producto"
        )
        amazon_offer = _insert_offer(
            conn, product_id=amazon_product, classification="normal"
        )
        amazon_outbox = _insert_outbox(
            conn,
            offer_id=amazon_offer,
            payload={"title": "Amazon", "current_price": 50.0},
        )

        # Lado mercadolibre: payload sin discount_percent → candidato.
        ml_product = _insert_product(
            conn, marketplace="mercadolibre", title="ML Producto"
        )
        ml_offer = _insert_offer(
            conn, product_id=ml_product, classification="normal"
        )
        ml_outbox = _insert_outbox(
            conn,
            offer_id=ml_offer,
            payload={
                "title": "ML",
                "current_price": 20.0,
                "previous_price": 25.0,
                # sin discount_percent
            },
        )

        task = OutboxMissingPrevPriceTask()

        # marketplace=mercadolibre → solo el outbox de mercadolibre.
        ml_candidates = task.select_candidates(
            conn, marketplace="mercadolibre", clock=fixed_clock
        )
        assert _select_ids(ml_candidates) == [ml_outbox]

        affected = task.apply(conn, ml_candidates, clock=fixed_clock)
        assert affected == 1

        ml_row = _fetch_outbox(conn, ml_outbox)
        assert ml_row["state"] == "discarded"

        # El outbox de amazon no se tocó.
        amazon_row = _fetch_outbox(conn, amazon_outbox)
        assert amazon_row["state"] == "pending"
        assert amazon_row["last_attempt_at"] is None

    def test_marketplace_all_includes_both_markets(self) -> None:
        """Sanity-check: ``marketplace='all'`` sí incluye ambos lados."""

        conn = _make_conn()

        amazon_product = _insert_product(conn, marketplace="amazon", title="A")
        amazon_offer = _insert_offer(
            conn, product_id=amazon_product, classification="normal"
        )
        amazon_outbox = _insert_outbox(
            conn,
            offer_id=amazon_offer,
            payload={"title": "A", "current_price": 10.0},
        )

        ml_product = _insert_product(
            conn, marketplace="mercadolibre", title="B"
        )
        ml_offer = _insert_offer(
            conn, product_id=ml_product, classification="normal"
        )
        ml_outbox = _insert_outbox(
            conn,
            offer_id=ml_offer,
            payload={"title": "B", "current_price": 20.0},
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == sorted([amazon_outbox, ml_outbox])


# ---------------------------------------------------------------------------
# 6) Exclusión price_error_confirmed (case-insensitive).
# ---------------------------------------------------------------------------


class TestPriceErrorConfirmedIsExcluded:
    """Filas con ``offers.classification='price_error_confirmed'`` (case-insensitive)
    NUNCA aparecen como candidatos, aunque el payload no tenga precios.

    Validates: Requirement 4.6
    """

    def test_lowercase_classification_excluded(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="PE_lc")
        # Offer marcado como price_error_confirmed: NO debe ser candidato.
        offer_id = _insert_offer(
            conn,
            product_id=product_id,
            classification="price_error_confirmed",
        )
        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"title": "PE_lc", "current_price": 5.0},
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_mixed_case_classification_excluded(self) -> None:
        """``Price_Error_Confirmed`` también se excluye (case-insensitive)."""

        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="PE_mc")
        offer_id = _insert_offer(
            conn,
            product_id=product_id,
            classification="Price_Error_Confirmed",
        )
        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"title": "PE_mc", "current_price": 5.0},
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_uppercase_classification_excluded(self) -> None:
        """``PRICE_ERROR_CONFIRMED`` también se excluye."""

        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="PE_uc")
        offer_id = _insert_offer(
            conn,
            product_id=product_id,
            classification="PRICE_ERROR_CONFIRMED",
        )
        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"title": "PE_uc", "current_price": 5.0},
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_excluded_row_is_not_mutated_in_apply_mode(self) -> None:
        """Aunque haya otros candidatos, la fila excluida sigue ``pending``."""

        conn = _make_conn()
        # Producto/offer normal → candidato.
        normal_product = _insert_product(
            conn, marketplace="amazon", title="Normal"
        )
        normal_offer = _insert_offer(
            conn, product_id=normal_product, classification="normal"
        )
        normal_outbox = _insert_outbox(
            conn,
            offer_id=normal_offer,
            payload={"title": "Normal", "current_price": 9.0},
        )

        # Producto/offer con classification price_error_confirmed → excluido.
        pe_product = _insert_product(
            conn, marketplace="amazon", title="Excluido"
        )
        pe_offer = _insert_offer(
            conn,
            product_id=pe_product,
            classification="price_error_confirmed",
        )
        pe_outbox = _insert_outbox(
            conn,
            offer_id=pe_offer,
            payload={"title": "Excluido", "current_price": 9.0},
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )
        assert _select_ids(candidates) == [normal_outbox]

        affected = task.apply(conn, candidates, clock=fixed_clock)
        assert affected == 1

        # El outbox excluido sigue ``pending`` y sin tocar.
        pe_row = _fetch_outbox(conn, pe_outbox)
        assert pe_row["state"] == "pending"
        assert pe_row["last_attempt_at"] is None


# ---------------------------------------------------------------------------
# 7) Truthiness del payload: None / 0 / "" cuentan como faltantes.
# ---------------------------------------------------------------------------


class TestPayloadTruthinessSemantics:
    """Paridad literal con ``not prev or not disc`` del script legacy.

    Cualquier valor falsy (``None``, ``0``, ``0.0``, ``""``) en
    ``previous_price`` o ``discount_percent`` cuenta como faltante.

    Validates: Requirement 4.5 (paridad de selección con script legacy)
    """

    def test_previous_price_none_is_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="A")
        offer_id = _insert_offer(
            conn, product_id=product_id, classification="normal"
        )
        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={
                "title": "A",
                "current_price": 5.0,
                "previous_price": None,
                "discount_percent": 20.0,
            },
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]

    def test_previous_price_zero_int_is_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="B")
        offer_id = _insert_offer(
            conn, product_id=product_id, classification="normal"
        )
        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={
                "title": "B",
                "current_price": 5.0,
                "previous_price": 0,
                "discount_percent": 20.0,
            },
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]

    def test_previous_price_zero_float_is_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="C")
        offer_id = _insert_offer(
            conn, product_id=product_id, classification="normal"
        )
        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={
                "title": "C",
                "current_price": 5.0,
                "previous_price": 0.0,
                "discount_percent": 20.0,
            },
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]

    def test_previous_price_empty_string_is_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="D")
        offer_id = _insert_offer(
            conn, product_id=product_id, classification="normal"
        )
        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={
                "title": "D",
                "current_price": 5.0,
                "previous_price": "",
                "discount_percent": 20.0,
            },
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]

    def test_discount_percent_none_is_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="E")
        offer_id = _insert_offer(
            conn, product_id=product_id, classification="normal"
        )
        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={
                "title": "E",
                "current_price": 5.0,
                "previous_price": 10.0,
                "discount_percent": None,
            },
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]

    def test_discount_percent_zero_is_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="F")
        offer_id = _insert_offer(
            conn, product_id=product_id, classification="normal"
        )
        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={
                "title": "F",
                "current_price": 5.0,
                "previous_price": 10.0,
                "discount_percent": 0,
            },
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]

    def test_discount_percent_empty_string_is_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="G")
        offer_id = _insert_offer(
            conn, product_id=product_id, classification="normal"
        )
        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={
                "title": "G",
                "current_price": 5.0,
                "previous_price": 10.0,
                "discount_percent": "",
            },
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]

    def test_payload_missing_both_keys_is_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="H")
        offer_id = _insert_offer(
            conn, product_id=product_id, classification="normal"
        )
        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"title": "H", "current_price": 5.0},
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]

    def test_payload_with_truthy_prev_and_disc_is_NOT_candidate(self) -> None:
        """Caso control: ``previous_price=10.0`` y ``discount_percent=20.0`` ⇒ NO candidato."""

        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="OK")
        offer_id = _insert_offer(
            conn, product_id=product_id, classification="normal"
        )
        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_valid_payload(),
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []


# ---------------------------------------------------------------------------
# 8) State guard: discarded/sent no aparecen como candidatos.
# ---------------------------------------------------------------------------


class TestStateGuardOnlyPending:
    """``select_candidates`` filtra ``o.state='pending'`` (paridad con script).

    Filas en estado ``discarded`` o ``sent`` nunca son candidatos,
    incluso si su payload no tiene precios.
    """

    def test_discarded_outbox_is_not_a_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="Disc")
        offer_id = _insert_offer(
            conn, product_id=product_id, classification="normal"
        )
        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"title": "Disc", "current_price": 5.0},
            state="discarded",
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_sent_outbox_is_not_a_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="Sent")
        offer_id = _insert_offer(
            conn, product_id=product_id, classification="normal"
        )
        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"title": "Sent", "current_price": 5.0},
            state="sent",
        )

        task = OutboxMissingPrevPriceTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []
