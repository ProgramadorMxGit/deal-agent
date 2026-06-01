"""Tests unitarios para ``ofertas_hunter.saneamiento.tasks.outbox_bad_discounts``.

Estos tests definen el contrato de la ``Saneamiento_Task``
``outbox-bad-discounts`` descrito en
``.kiro/specs/bot-saneamiento-vps/design.md`` (sección
``Components and Interfaces`` → ``OutboxBadDiscountsTask``). Se escriben
antes de la implementación (TDD-RED): la tarea 6.2 hará que estos
tests pasen.

Paridad estricta con ``scripts/_vps_cleanup_bad_discounts.py``:

- SELECT con join ``outbox o JOIN offers of JOIN products p`` y
  filtro ``o.state='pending'`` + ``marketplace_clause``.
- ``json_extract`` de ``current_price``, ``previous_price`` y
  ``discount_percent`` desde ``message_payload_json``.
- Filtrado en Python:

    if cur is None or prev is None or disc is None:
        continue                 # cualquier None ⇒ NO candidato
    if prev <= 0 or cur <= 0 or prev <= cur:
        yield candidate          # signos inválidos o prev<=cur ⇒ candidato
        continue
    real = (prev - cur) / prev * 100.0
    if abs(real - disc) > 3.0:
        yield candidate          # mismatch > 3 puntos ⇒ candidato

  El umbral es **estrictamente** ``> 3.0``: una diferencia exacta de
  ``3.0`` puntos NO es candidato (paridad literal con el script).

- ``writes_last_attempt_at = True``: el ``apply`` heredado actualiza
  ``last_attempt_at`` con el ``now_iso`` del clock inyectado
  (Requirement 4.4, 4.8).

Cobertura (Requirement 8.5):

1. Caso vacío: DB sin filas → ``inspected=0`` y ``affected=0``.
2. Caso Dry_Run con candidatos: snapshot byte-idéntico antes/después
   (Requirements 5.1, 5.2, 5.3, 5.4).
3. Caso Apply_Mode con candidatos: ``state='discarded'`` y
   ``last_attempt_at = FIXED_ISO`` (Requirements 4.4, 4.8).
4. Caso idempotencia: segunda ``Saneamiento_Run`` reporta ``affected=0``
   (Requirement 5.1).
5. Caso ``--marketplace=amazon``: outbox de ``mercadolibre`` no se
   incluye como candidato (Requirement 4.7).
6. Caso ``prev <= cur``: candidato (también ``prev == cur``).
7. Caso ``prev <= 0`` o ``cur <= 0``: candidato.
8. Caso umbral ``> 3.0`` vs ``<= 3.0``: candidato sólo cuando la
   diferencia es estrictamente ``> 3.0`` (paridad numérica con el script).
9. Caso fila con cualquier campo ``None``: NO candidato.
10. Caso state guard: ``discarded``/``sent`` no aparecen como candidatos.

Reloj fijo (paridad con tests de ``outbox-duplicates`` y
``outbox-missing-prev-price``):

    FIXED_DT  = datetime(2026, 5, 29, 12, 0, 0, 0, tzinfo=timezone.utc)
    FIXED_ISO = "2026-05-29T12:00:00.000Z"
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from ofertas_hunter.saneamiento.tasks.outbox_bad_discounts import (
    OutboxBadDiscountsTask,
)


# ---------------------------------------------------------------------------
# Reloj determinista — paridad con las property tests del spec.
# ---------------------------------------------------------------------------


FIXED_DT = datetime(2026, 5, 29, 12, 0, 0, 0, tzinfo=timezone.utc)
FIXED_ISO = "2026-05-29T12:00:00.000Z"
"""ISO-8601 ms con sufijo ``Z`` que ``apply`` debe escribir en
``last_attempt_at`` cuando muta una fila con descuento inconsistente."""


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


def _bad_discount_payload(
    *,
    title: str = "Producto",
    current_price: Any = 100.0,
    previous_price: Any = 50.0,
    discount_percent: Any = 10.0,
) -> dict[str, Any]:
    """Payload con prev<=cur por defecto (candidato típico).

    ``current_price=100, previous_price=50, discount_percent=10`` cumple
    ``prev <= cur`` (50 <= 100), por lo que cualquier task correcta lo
    debe identificar como candidato sin necesidad de calcular ``real``.
    """

    return {
        "title": title,
        "current_price": current_price,
        "previous_price": previous_price,
        "discount_percent": discount_percent,
    }


def _valid_discount_payload(
    *,
    title: str = "Producto OK",
    current_price: float = 80.0,
    previous_price: float = 100.0,
    discount_percent: float = 20.0,
) -> dict[str, Any]:
    """Payload con descuento consistente (NO candidato).

    Default: ``cur=80, prev=100, disc=20`` → ``real = 20.0``,
    ``abs(real - disc) = 0 ≤ 3.0`` ⇒ NO candidato.
    """

    return {
        "title": title,
        "current_price": current_price,
        "previous_price": previous_price,
        "discount_percent": discount_percent,
    }


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
        task = OutboxBadDiscountsTask()

        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_apply_with_empty_list_returns_zero(self) -> None:
        conn = _make_conn()
        task = OutboxBadDiscountsTask()

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

        # prev=50 <= cur=100 ⇒ candidato (sin necesidad de calcular real).
        candidate_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_bad_discount_payload(
                title="Auriculares",
                current_price=100.0,
                previous_price=50.0,
                discount_percent=10.0,
            ),
        )

        snapshot_before = _take_snapshot(conn)

        task = OutboxBadDiscountsTask()
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

    Validates: Requirements 4.4, 4.8
    """

    def test_candidate_becomes_discarded_with_last_attempt_at(self) -> None:
        conn = _make_conn()

        product_id = _insert_product(
            conn, marketplace="amazon", title="Cargador"
        )
        offer_id = _insert_offer(conn, product_id=product_id)

        # Mismatch grande: cur=80, prev=100 ⇒ real=20.0; disc=50 ⇒ |50-20|=30 > 3.
        candidate_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_bad_discount_payload(
                title="Cargador",
                current_price=80.0,
                previous_price=100.0,
                discount_percent=50.0,
            ),
        )

        task = OutboxBadDiscountsTask()
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
        """Sanity-check: una fila con descuento consistente queda intacta."""

        conn = _make_conn()

        product_id = _insert_product(conn, marketplace="amazon", title="OK")
        offer_id = _insert_offer(conn, product_id=product_id)

        # cur=80, prev=100 ⇒ real=20; disc=20 ⇒ |20-20|=0 ≤ 3 ⇒ NO candidato.
        ok_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_valid_discount_payload(),
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )
        affected = task.apply(conn, candidates, clock=fixed_clock)

        assert candidates == []
        assert affected == 0

        row = _fetch_outbox(conn, ok_outbox)
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

        # Candidato típico: prev<=cur.
        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_bad_discount_payload(
                title="Mouse",
                current_price=100.0,
                previous_price=50.0,
                discount_percent=10.0,
            ),
        )

        task = OutboxBadDiscountsTask()

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
# 5) Caso filtro --marketplace=amazon: outbox de mercadolibre no se afecta.
# ---------------------------------------------------------------------------


class TestMarketplaceFilterAmazonExcludesMercadolibre:
    """Con ``--marketplace=amazon`` los outbox de ``mercadolibre`` se excluyen.

    Validates: Requirement 4.7
    """

    def test_mercadolibre_outbox_is_not_in_amazon_candidates(self) -> None:
        conn = _make_conn()

        # Lado amazon: candidato (prev<=cur).
        amazon_product = _insert_product(
            conn, marketplace="amazon", title="Amazon Producto"
        )
        amazon_offer = _insert_offer(conn, product_id=amazon_product)
        amazon_outbox = _insert_outbox(
            conn,
            offer_id=amazon_offer,
            payload=_bad_discount_payload(
                title="Amazon",
                current_price=100.0,
                previous_price=50.0,
                discount_percent=10.0,
            ),
        )

        # Lado mercadolibre: también candidato (mismatch > 3pp), pero
        # con filtro ``amazon`` debe quedar fuera.
        ml_product = _insert_product(
            conn, marketplace="mercadolibre", title="ML Producto"
        )
        ml_offer = _insert_offer(conn, product_id=ml_product)
        ml_outbox = _insert_outbox(
            conn,
            offer_id=ml_offer,
            payload=_bad_discount_payload(
                title="ML",
                current_price=80.0,
                previous_price=100.0,
                discount_percent=50.0,
            ),
        )

        task = OutboxBadDiscountsTask()

        # marketplace=amazon → solo el outbox de amazon.
        amazon_candidates = task.select_candidates(
            conn, marketplace="amazon", clock=fixed_clock
        )
        assert _select_ids(amazon_candidates) == [amazon_outbox]

        affected = task.apply(conn, amazon_candidates, clock=fixed_clock)
        assert affected == 1

        amazon_row = _fetch_outbox(conn, amazon_outbox)
        assert amazon_row["state"] == "discarded"
        assert amazon_row["last_attempt_at"] == FIXED_ISO

        # El outbox de mercadolibre no se tocó.
        ml_row = _fetch_outbox(conn, ml_outbox)
        assert ml_row["state"] == "pending"
        assert ml_row["last_attempt_at"] is None

    def test_marketplace_all_includes_both_markets(self) -> None:
        """Sanity-check: ``marketplace='all'`` sí incluye ambos lados."""

        conn = _make_conn()

        amazon_product = _insert_product(conn, marketplace="amazon", title="A")
        amazon_offer = _insert_offer(conn, product_id=amazon_product)
        amazon_outbox = _insert_outbox(
            conn,
            offer_id=amazon_offer,
            payload=_bad_discount_payload(
                title="A",
                current_price=100.0,
                previous_price=50.0,
                discount_percent=10.0,
            ),
        )

        ml_product = _insert_product(
            conn, marketplace="mercadolibre", title="B"
        )
        ml_offer = _insert_offer(conn, product_id=ml_product)
        ml_outbox = _insert_outbox(
            conn,
            offer_id=ml_offer,
            payload=_bad_discount_payload(
                title="B",
                current_price=80.0,
                previous_price=100.0,
                discount_percent=50.0,
            ),
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == sorted([amazon_outbox, ml_outbox])


# ---------------------------------------------------------------------------
# 6) Caso prev <= cur: candidato.
# ---------------------------------------------------------------------------


class TestPrevLessOrEqualCurrentIsCandidate:
    """``prev <= cur`` ⇒ candidato (paridad con script).

    Cubre el caso ``prev == cur`` (igualdad) y ``prev < cur`` (precio
    "anterior" menor que el actual, lo cual es absurdo en un descuento).
    """

    def test_prev_equal_cur_is_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="Eq")
        offer_id = _insert_offer(conn, product_id=product_id)

        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_bad_discount_payload(
                title="Eq",
                current_price=10.0,
                previous_price=10.0,
                discount_percent=0.0,
            ),
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]

    def test_prev_less_than_cur_is_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="Lt")
        offer_id = _insert_offer(conn, product_id=product_id)

        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_bad_discount_payload(
                title="Lt",
                current_price=10.0,
                previous_price=8.0,
                discount_percent=10.0,
            ),
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]


# ---------------------------------------------------------------------------
# 7) Caso prev <= 0 o cur <= 0: candidato.
# ---------------------------------------------------------------------------


class TestPrevOrCurNonPositiveIsCandidate:
    """``prev <= 0`` o ``cur <= 0`` ⇒ candidato.

    El script legacy clasifica estos casos como ``prev<=cur`` (la
    primera rama del ``if``) porque ``0 >= 5`` se evalúa antes que
    cualquier cálculo de ``real``. Desde la perspectiva del contrato
    del task, lo que importa es que la fila aparezca como candidato
    independientemente del valor de ``discount_percent``.
    """

    def test_prev_zero_is_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="P0")
        offer_id = _insert_offer(conn, product_id=product_id)

        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_bad_discount_payload(
                title="P0",
                current_price=5.0,
                previous_price=0.0,
                discount_percent=20.0,
            ),
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]

    def test_prev_negative_is_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="Pn")
        offer_id = _insert_offer(conn, product_id=product_id)

        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_bad_discount_payload(
                title="Pn",
                current_price=5.0,
                previous_price=-1.0,
                discount_percent=20.0,
            ),
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]

    def test_cur_zero_is_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="C0")
        offer_id = _insert_offer(conn, product_id=product_id)

        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_bad_discount_payload(
                title="C0",
                current_price=0.0,
                previous_price=10.0,
                discount_percent=50.0,
            ),
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]

    def test_cur_negative_is_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="Cn")
        offer_id = _insert_offer(conn, product_id=product_id)

        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_bad_discount_payload(
                title="Cn",
                current_price=-1.0,
                previous_price=10.0,
                discount_percent=20.0,
            ),
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]


# ---------------------------------------------------------------------------
# 8) Umbral abs(real - disc) > 3.0: estrictamente mayor.
# ---------------------------------------------------------------------------


class TestDiscountMismatchBoundary:
    """Paridad numérica con ``_vps_cleanup_bad_discounts.py``.

    Para ``cur=80, prev=100`` se cumple ``real = (100-80)/100*100 = 20.0``.
    El umbral es **estrictamente** ``> 3.0``: ``abs(real - disc) == 3.0``
    NO es candidato; ``abs(real - disc) > 3.0`` sí lo es.
    """

    def test_difference_zero_is_not_candidate(self) -> None:
        """``cur=80, prev=100, disc=20`` ⇒ ``|20-20|=0 ≤ 3`` ⇒ NO candidato."""

        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="D0")
        offer_id = _insert_offer(conn, product_id=product_id)

        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_valid_discount_payload(
                current_price=80.0,
                previous_price=100.0,
                discount_percent=20.0,
            ),
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_difference_exactly_three_is_not_candidate_above(self) -> None:
        """``cur=80, prev=100, disc=23`` ⇒ ``|23-20|=3.0 == 3.0`` ⇒ NO candidato.

        El umbral es estrictamente ``> 3.0``: el caso límite por arriba
        (``disc`` tres puntos por encima de ``real``) no debe disparar.
        """

        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="D3a")
        offer_id = _insert_offer(conn, product_id=product_id)

        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_bad_discount_payload(
                title="D3a",
                current_price=80.0,
                previous_price=100.0,
                discount_percent=23.0,
            ),
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_difference_exactly_three_is_not_candidate_below(self) -> None:
        """``cur=80, prev=100, disc=17`` ⇒ ``|17-20|=3.0`` ⇒ NO candidato.

        Caso límite por abajo (``disc`` tres puntos por debajo de
        ``real``): tampoco debe disparar.
        """

        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="D3b")
        offer_id = _insert_offer(conn, product_id=product_id)

        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_bad_discount_payload(
                title="D3b",
                current_price=80.0,
                previous_price=100.0,
                discount_percent=17.0,
            ),
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_difference_greater_than_three_above_is_candidate(self) -> None:
        """``cur=80, prev=100, disc=23.5`` ⇒ ``|23.5-20|=3.5 > 3`` ⇒ candidato."""

        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="D35")
        offer_id = _insert_offer(conn, product_id=product_id)

        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_bad_discount_payload(
                title="D35",
                current_price=80.0,
                previous_price=100.0,
                discount_percent=23.5,
            ),
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]

    def test_difference_greater_than_three_below_is_candidate(self) -> None:
        """``cur=80, prev=100, disc=16`` ⇒ ``|16-20|=4 > 3`` ⇒ candidato."""

        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="D4")
        offer_id = _insert_offer(conn, product_id=product_id)

        outbox_id = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_bad_discount_payload(
                title="D4",
                current_price=80.0,
                previous_price=100.0,
                discount_percent=16.0,
            ),
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == [outbox_id]


# ---------------------------------------------------------------------------
# 9) Caso fila con cualquier campo None: NO candidato.
# ---------------------------------------------------------------------------


class TestAnyNoneFieldIsNotCandidate:
    """Paridad con script legacy: ``cur is None or prev is None or disc is None``
    ⇒ ``continue`` (NO candidato), aunque otros campos sean inválidos.

    Cubre payload con campos explícitamente ``None`` (que ``json_extract``
    devuelve como ``NULL``) y payload con campos ausentes (``json_extract``
    devuelve ``NULL`` también).
    """

    def test_current_price_none_is_not_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="Nc")
        offer_id = _insert_offer(conn, product_id=product_id)

        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={
                "title": "Nc",
                "current_price": None,
                "previous_price": 100.0,
                "discount_percent": 20.0,
            },
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_previous_price_none_is_not_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="Np")
        offer_id = _insert_offer(conn, product_id=product_id)

        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={
                "title": "Np",
                "current_price": 80.0,
                "previous_price": None,
                "discount_percent": 20.0,
            },
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_discount_percent_none_is_not_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="Nd")
        offer_id = _insert_offer(conn, product_id=product_id)

        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={
                "title": "Nd",
                "current_price": 80.0,
                "previous_price": 100.0,
                "discount_percent": None,
            },
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_all_three_none_is_not_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="All")
        offer_id = _insert_offer(conn, product_id=product_id)

        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={
                "title": "All",
                "current_price": None,
                "previous_price": None,
                "discount_percent": None,
            },
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_payload_missing_all_keys_is_not_candidate(self) -> None:
        """Payload sin las claves ⇒ ``json_extract`` devuelve NULL ⇒ no candidato.

        Paridad con el script: ``r["cur"] is None`` se cumple para
        claves ausentes igual que para ``None`` explícito.
        """

        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="Miss")
        offer_id = _insert_offer(conn, product_id=product_id)

        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"title": "Miss"},  # sin precios ni discount
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []


# ---------------------------------------------------------------------------
# 10) State guard: discarded/sent no aparecen como candidatos.
# ---------------------------------------------------------------------------


class TestStateGuardOnlyPending:
    """``select_candidates`` filtra ``o.state='pending'`` (paridad con script).

    Filas en estado ``discarded`` o ``sent`` nunca son candidatos,
    incluso si su payload tiene un descuento inconsistente.
    """

    def test_discarded_outbox_is_not_a_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="Disc")
        offer_id = _insert_offer(conn, product_id=product_id)

        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_bad_discount_payload(
                title="Disc",
                current_price=100.0,
                previous_price=50.0,
                discount_percent=10.0,
            ),
            state="discarded",
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_sent_outbox_is_not_a_candidate(self) -> None:
        conn = _make_conn()
        product_id = _insert_product(conn, marketplace="amazon", title="Sent")
        offer_id = _insert_offer(conn, product_id=product_id)

        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload=_bad_discount_payload(
                title="Sent",
                current_price=100.0,
                previous_price=50.0,
                discount_percent=10.0,
            ),
            state="sent",
        )

        task = OutboxBadDiscountsTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []
