"""Tests unitarios para ``ofertas_hunter.saneamiento.tasks.outbox_duplicates``.

Estos tests definen el contrato de la ``Saneamiento_Task``
``outbox-duplicates`` descrito en
``.kiro/specs/bot-saneamiento-vps/design.md`` (sección
``Components and Interfaces`` → ``OutboxDuplicatesTask``). Se escriben
antes de la implementación (TDD-RED): la tarea 4.2 hará que estos
tests pasen.

Cobertura (Requirement 8.5):

1. Caso vacío: DB sin filas → ``inspected=0`` y ``affected=0``.
2. Caso Dry_Run con candidatos: ``inspected>0``, ``affected=0`` y la
   DB queda byte-idéntica antes/después (Requirements 5.1, 5.2, 5.3,
   5.4).
3. Caso Apply_Mode con candidatos: las filas duplicadas pasan a
   ``state='discarded'`` y ``last_attempt_at`` se actualiza al
   ``now_iso`` derivado del ``clock`` inyectado (Requirements 4.3,
   4.8).
4. Caso idempotencia: segunda invocación de ``select_candidates`` y
   ``apply`` reporta ``affected=0`` (Requirement 5.1).
5. Caso ``--marketplace=amazon``: outbox de ``mercadolibre`` no se
   incluye como candidato (Requirement 4.7).
6. Dedup por ``outbox.id`` cuando una misma fila matchea por
   ``item_id`` y ``asin`` simultáneamente (paridad con
   ``scripts/_vps_cleanup_duplicates.py``).

Reloj fijo:

    FIXED_DT  = datetime(2026, 5, 29, 12, 0, 0, 0, tzinfo=timezone.utc)
    FIXED_ISO = "2026-05-29T12:00:00.000Z"

El cutoff de 48h calculado por la task contra ese clock es
``2026-05-27T12:00:00.000Z``, así que cualquier
``published_messages.sent_at`` posterior a ese instante cuenta como
publicado en las últimas 48 horas.

Schema en :memory:: paridad con ``migrations/001_init.sql`` para las
tablas que toca esta task (``products``, ``offers``, ``outbox``,
``published_messages``). Se omiten FK enforcement y otras tablas.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from ofertas_hunter.saneamiento.tasks.outbox_duplicates import (
    OutboxDuplicatesTask,
)


# ---------------------------------------------------------------------------
# Reloj determinista — paridad con las property tests del spec.
# ---------------------------------------------------------------------------


FIXED_DT = datetime(2026, 5, 29, 12, 0, 0, 0, tzinfo=timezone.utc)
FIXED_ISO = "2026-05-29T12:00:00.000Z"
"""ISO-8601 ms con sufijo ``Z`` que ``apply`` debe escribir en
``last_attempt_at`` cuando muta una fila duplicada."""


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

CREATE TABLE published_messages (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    outbox_id          INTEGER,
    offer_id           INTEGER,
    sent_at            TEXT NOT NULL,
    success            INTEGER NOT NULL,
    evolution_response TEXT,
    message_text       TEXT NOT NULL DEFAULT '',
    media_url          TEXT
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


def _insert_published(
    conn: sqlite3.Connection,
    *,
    outbox_id: int | None,
    offer_id: int | None,
    sent_at: str = "2026-05-29T11:30:00.000Z",
    success: int = 1,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO published_messages (
            outbox_id, offer_id, sent_at, success, message_text
        ) VALUES (?, ?, ?, ?, '')
        """,
        (outbox_id, offer_id, sent_at, success),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Snapshots — para verificar que Dry_Run no muta la DB.
# ---------------------------------------------------------------------------


_TABLES_TO_SNAPSHOT = (
    "products",
    "offers",
    "outbox",
    "published_messages",
)


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
        task = OutboxDuplicatesTask()

        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []

    def test_apply_with_empty_list_returns_zero(self) -> None:
        conn = _make_conn()
        task = OutboxDuplicatesTask()

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

        # Producto + offer en amazon.
        product_id = _insert_product(conn, marketplace="amazon", title="Auriculares")
        offer_id = _insert_offer(conn, product_id=product_id)

        # Outbox publicado (referenciado por published_messages).
        published_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"item_id": "ABC", "title": "Auriculares"},
            state="sent",
        )
        _insert_published(
            conn,
            outbox_id=published_outbox,
            offer_id=offer_id,
            sent_at="2026-05-29T11:00:00.000Z",
            success=1,
        )

        # Outbox pending duplicado del mismo item_id="ABC".
        duplicate_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"item_id": "ABC", "title": "Auriculares"},
            state="pending",
        )

        snapshot_before = _take_snapshot(conn)

        task = OutboxDuplicatesTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        # Hay exactamente un candidato: el outbox pending duplicado.
        assert _select_ids(candidates) == [duplicate_outbox]

        # La DB no se tocó por la sola selección (Dry_Run).
        snapshot_after = _take_snapshot(conn)
        assert snapshot_before == snapshot_after


# ---------------------------------------------------------------------------
# 3) Caso Apply_Mode con candidatos: filas afectadas → discarded + last_attempt_at.
# ---------------------------------------------------------------------------


class TestApplyModeMarksDuplicatesAsDiscarded:
    """``apply`` muta sólo el outbox duplicado pending.

    Validates: Requirements 4.3, 4.8
    """

    def test_duplicate_outbox_becomes_discarded_with_last_attempt_at(self) -> None:
        conn = _make_conn()

        product_id = _insert_product(conn, marketplace="amazon", title="Cargador")
        offer_id = _insert_offer(conn, product_id=product_id)

        published_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"item_id": "XYZ"},
            state="sent",
        )
        _insert_published(
            conn,
            outbox_id=published_outbox,
            offer_id=offer_id,
            sent_at="2026-05-29T11:30:00.000Z",
            success=1,
        )

        duplicate_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"item_id": "XYZ"},
            state="pending",
        )

        task = OutboxDuplicatesTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )
        affected = task.apply(conn, candidates, clock=fixed_clock)

        assert affected == 1

        # Duplicado: state='discarded' + last_attempt_at = FIXED_ISO.
        dup_row = _fetch_outbox(conn, duplicate_outbox)
        assert dup_row["state"] == "discarded"
        assert dup_row["last_attempt_at"] == FIXED_ISO

        # El outbox ya publicado (state='sent') no se incluyó como candidato
        # y queda intacto: state='sent', last_attempt_at=NULL.
        published_row = _fetch_outbox(conn, published_outbox)
        assert published_row["state"] == "sent"
        assert published_row["last_attempt_at"] is None


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

        published_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"item_id": "M1"},
            state="sent",
        )
        _insert_published(
            conn,
            outbox_id=published_outbox,
            offer_id=offer_id,
            sent_at="2026-05-29T11:45:00.000Z",
            success=1,
        )

        duplicate_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"item_id": "M1"},
            state="pending",
        )

        task = OutboxDuplicatesTask()

        # Primera run: selecciona y aplica.
        first_candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )
        first_affected = task.apply(conn, first_candidates, clock=fixed_clock)
        assert first_affected == 1
        assert _select_ids(first_candidates) == [duplicate_outbox]

        # Segunda run: el duplicado ya está en state='discarded', así que
        # no debe aparecer como candidato (la task filtra state='pending').
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

        # Lado amazon: producto + offer + outbox sent (publicado) + outbox pending dup.
        amazon_product = _insert_product(
            conn, marketplace="amazon", title="Amazon Producto"
        )
        amazon_offer = _insert_offer(conn, product_id=amazon_product)
        amazon_published_outbox = _insert_outbox(
            conn,
            offer_id=amazon_offer,
            payload={"item_id": "DUP", "asin": "DUP"},
            state="sent",
        )
        _insert_published(
            conn,
            outbox_id=amazon_published_outbox,
            offer_id=amazon_offer,
            sent_at="2026-05-29T11:00:00.000Z",
            success=1,
        )
        amazon_duplicate_outbox = _insert_outbox(
            conn,
            offer_id=amazon_offer,
            payload={"item_id": "DUP", "asin": "DUP"},
            state="pending",
        )

        # Lado mercadolibre: producto + offer + outbox pending con mismo item_id.
        ml_product = _insert_product(
            conn, marketplace="mercadolibre", title="ML Producto"
        )
        ml_offer = _insert_offer(conn, product_id=ml_product)
        ml_pending_outbox = _insert_outbox(
            conn,
            offer_id=ml_offer,
            payload={"item_id": "DUP"},
            state="pending",
        )

        task = OutboxDuplicatesTask()

        # marketplace=amazon → solo el duplicado de amazon.
        amazon_candidates = task.select_candidates(
            conn, marketplace="amazon", clock=fixed_clock
        )
        assert _select_ids(amazon_candidates) == [amazon_duplicate_outbox]

        # Apply: solo el duplicado de amazon muta.
        affected = task.apply(conn, amazon_candidates, clock=fixed_clock)
        assert affected == 1

        amazon_dup_row = _fetch_outbox(conn, amazon_duplicate_outbox)
        assert amazon_dup_row["state"] == "discarded"
        assert amazon_dup_row["last_attempt_at"] == FIXED_ISO

        # El outbox de mercadolibre no se tocó.
        ml_row = _fetch_outbox(conn, ml_pending_outbox)
        assert ml_row["state"] == "pending"
        assert ml_row["last_attempt_at"] is None

    def test_marketplace_all_includes_both_markets(self) -> None:
        """Sanity-check: ``marketplace='all'`` sí incluye ambos lados.

        Confirma que la diferencia observada en el test anterior viene
        del filtro y no de un bug en los fixtures.
        """

        conn = _make_conn()

        amazon_product = _insert_product(conn, marketplace="amazon", title="A")
        amazon_offer = _insert_offer(conn, product_id=amazon_product)
        amazon_pub = _insert_outbox(
            conn,
            offer_id=amazon_offer,
            payload={"item_id": "DUP"},
            state="sent",
        )
        _insert_published(
            conn,
            outbox_id=amazon_pub,
            offer_id=amazon_offer,
            sent_at="2026-05-29T11:00:00.000Z",
            success=1,
        )
        amazon_dup = _insert_outbox(
            conn,
            offer_id=amazon_offer,
            payload={"item_id": "DUP"},
            state="pending",
        )

        ml_product = _insert_product(conn, marketplace="mercadolibre", title="B")
        ml_offer = _insert_offer(conn, product_id=ml_product)
        ml_dup = _insert_outbox(
            conn,
            offer_id=ml_offer,
            payload={"item_id": "DUP"},
            state="pending",
        )

        task = OutboxDuplicatesTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert _select_ids(candidates) == sorted([amazon_dup, ml_dup])


# ---------------------------------------------------------------------------
# 6) Dedup por outbox.id cuando un row matchea por item_id Y asin.
# ---------------------------------------------------------------------------


class TestDedupByOutboxIdWhenItemIdAndAsinBothMatch:
    """Si una misma fila pending tiene ``item_id`` y ``asin`` iguales y
    ambos coinciden con un id publicado, debe aparecer una sola vez en
    los candidatos (paridad con el script ``_vps_cleanup_duplicates.py``).
    """

    def test_outbox_with_matching_item_id_and_asin_appears_once(self) -> None:
        conn = _make_conn()

        product_id = _insert_product(conn, marketplace="amazon", title="Tablet")
        offer_id = _insert_offer(conn, product_id=product_id)

        # published_messages que cubre tanto item_id como asin = "DUP".
        published_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"item_id": "DUP", "asin": "DUP"},
            state="sent",
        )
        _insert_published(
            conn,
            outbox_id=published_outbox,
            offer_id=offer_id,
            sent_at="2026-05-29T11:30:00.000Z",
            success=1,
        )

        # Outbox pending cuyo payload cumple ambos: item_id="DUP" y asin="DUP".
        dup_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"item_id": "DUP", "asin": "DUP"},
            state="pending",
        )

        task = OutboxDuplicatesTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        ids = [c.outbox_id for c in candidates]
        # Dedup por outbox.id: aparece una sola vez aunque matchee dos veces.
        assert ids == [dup_outbox]
        assert len(candidates) == 1


# ---------------------------------------------------------------------------
# 7) Cutoff de 48h: published_messages fuera del cutoff no genera duplicados.
# ---------------------------------------------------------------------------


class TestCutoff48HoursBoundary:
    """Sólo cuentan los ``published_messages`` con ``sent_at >= now-48h``."""

    def test_published_older_than_48h_does_not_yield_candidates(self) -> None:
        conn = _make_conn()

        product_id = _insert_product(conn, marketplace="amazon", title="Cable")
        offer_id = _insert_offer(conn, product_id=product_id)

        # Publicado hace 60h respecto a FIXED_DT → fuera del cutoff de 48h.
        old_outbox = _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"item_id": "OLD"},
            state="sent",
        )
        _insert_published(
            conn,
            outbox_id=old_outbox,
            offer_id=offer_id,
            sent_at="2026-05-27T00:00:00.000Z",  # cutoff es 2026-05-27T12:00Z
            success=1,
        )

        # Outbox pending con item_id="OLD" → no debería ser candidato.
        _insert_outbox(
            conn,
            offer_id=offer_id,
            payload={"item_id": "OLD"},
            state="pending",
        )

        task = OutboxDuplicatesTask()
        candidates = task.select_candidates(
            conn, marketplace="all", clock=fixed_clock
        )

        assert candidates == []
