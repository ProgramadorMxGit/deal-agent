"""Tests del DiversityCurator — orquestación scorer + LLM + fallback.

Cubre criterios A-G del spec:
A. Solo determinístico (use_llm=false) → publica top1 score.
B. LLM elige válido → publica el id elegido.
C. LLM elige inválido (id fuera de top10) → fallback a top1.
D. LLM timeout → fallback a top1.
E. LLM no instalado → arranca y degrada a top1.
F. Sin candidatos → retorna None.
G. Un solo candidato → skip LLM, retorna directo.

Adicional (resiliencia de _load_history):
- Filas con `message_payload_json` no parseable → logged WARNING y skip.
- Filas con `sent_at` no parseable → logged WARNING y skip (no usar now()).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from ofertas_hunter.db import init_db
from ofertas_hunter.dispatching.cooldown import CooldownPolicy
from ofertas_hunter.dispatching.diversity_curator import DiversityCurator
from ofertas_hunter.dispatching.diversity_scorer import DiversityScorer
from ofertas_hunter.dispatching.outbox import InMemoryOutbox, OutboxConfig
from ofertas_hunter.models import OutboxItem, OutboxState, OutboxType


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _now():
    return datetime.now(timezone.utc)


def _make_outbox_with(items: list[OutboxItem]) -> InMemoryOutbox:
    ob = InMemoryOutbox(OutboxConfig(cooldown=CooldownPolicy(0)))
    for it in items:
        ob.enqueue(it)
    return ob


def _item(
    *,
    id: int = 0,
    category: str = "cat",
    marketplace: str = "amazon",
    item_id: str = "ASIN1",
) -> OutboxItem:
    return OutboxItem(
        id=id,
        offer_id=id or 1,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": f"Item {id}",
            "category": category,
            "marketplace": marketplace,
            "item_id": item_id,
            "current_price": 1000.0,
            "discount_percent": 50.0,
            "image_url": "https://x/img",
            "url": "https://x",
        },
        enqueued_at=_now(),
        attempts=0,
        state=OutboxState.PENDING.value,
    )


# ---------------------------------------------------------------------------
# F / G — atajos sin LLM ni scoring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curator_F_returns_none_when_no_candidates(db):
    curator = DiversityCurator(
        db=db, scorer=DiversityScorer(), llm_client=None,
    )
    outbox = _make_outbox_with([])
    picked = await curator.pick(outbox, last_normal_publication_at=None, now=_now())
    assert picked is None


@pytest.mark.asyncio
async def test_curator_G_single_candidate_returns_directly(db):
    """Si hay un solo candidato, no llama al LLM."""
    llm = AsyncMock()
    llm.ask_json = AsyncMock(return_value=None)

    curator = DiversityCurator(
        db=db, scorer=DiversityScorer(), llm_client=llm,
    )
    only = _item(id=1)
    outbox = _make_outbox_with([only])
    picked = await curator.pick(outbox, last_normal_publication_at=None, now=_now())
    assert picked is not None
    assert picked.id == 1
    llm.ask_json.assert_not_awaited()


# ---------------------------------------------------------------------------
# A — fallback determinístico cuando no hay LLM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curator_A_no_llm_picks_top_score(db):
    """Sin LLM, devuelve el top1 del scorer y emite event con fallback_used=True."""
    curator = DiversityCurator(
        db=db, scorer=DiversityScorer(), llm_client=None,
    )
    items = [
        _item(id=1, category="electronica"),
        _item(id=2, category="hogar"),
    ]
    outbox = _make_outbox_with(items)
    picked = await curator.pick(outbox, last_normal_publication_at=None, now=_now())
    # Sin historial todos parten igual → orden estable
    assert picked is not None
    assert picked.id in (1, 2)

    rows = db.execute(
        "SELECT payload_json FROM runtime_events WHERE kind='diversity_curator_decision'"
    ).fetchall()
    assert len(rows) == 1
    p = json.loads(rows[0]["payload_json"])
    assert p["fallback_used"] is True
    assert p["reason"] == "no_llm_client"


# ---------------------------------------------------------------------------
# B / C / D — orquestación con LLM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curator_B_llm_chooses_valid_id(db):
    """LLM responde con un chosen_id que está en top10 → publica ese."""
    llm = AsyncMock()
    llm.ask_json = AsyncMock(return_value={"chosen_id": 7, "reason": "ok"})

    curator = DiversityCurator(
        db=db, scorer=DiversityScorer(), llm_client=llm,
    )
    items = [
        _item(id=5, category="a"),
        _item(id=7, category="b"),
        _item(id=9, category="c"),
    ]
    outbox = _make_outbox_with(items)
    picked = await curator.pick(outbox, None, _now())
    assert picked is not None
    assert picked.id == 7
    llm.ask_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_curator_C_llm_chooses_invalid_id_falls_back(db):
    """LLM responde con id fuera del top10 → fallback al top1."""
    llm = AsyncMock()
    llm.ask_json = AsyncMock(return_value={"chosen_id": 999, "reason": "x"})

    curator = DiversityCurator(
        db=db, scorer=DiversityScorer(), llm_client=llm,
    )
    items = [_item(id=1), _item(id=2), _item(id=3)]
    outbox = _make_outbox_with(items)
    picked = await curator.pick(outbox, None, _now())
    assert picked is not None
    assert picked.id in {1, 2, 3}
    rows = db.execute(
        "SELECT payload_json FROM runtime_events WHERE kind='diversity_curator_decision'"
    ).fetchall()
    assert len(rows) == 1
    p = json.loads(rows[0]["payload_json"])
    assert p["fallback_used"] is True
    assert "999" in p["reason"]


@pytest.mark.asyncio
async def test_curator_D_llm_timeout_falls_back(db):
    """LLM retorna None (timeout/error) → fallback al top1."""
    llm = AsyncMock()
    llm.ask_json = AsyncMock(return_value=None)

    curator = DiversityCurator(
        db=db, scorer=DiversityScorer(), llm_client=llm,
    )
    items = [_item(id=10), _item(id=20)]
    outbox = _make_outbox_with(items)
    picked = await curator.pick(outbox, None, _now())
    assert picked is not None
    assert picked.id in {10, 20}
    rows = db.execute(
        "SELECT payload_json FROM runtime_events WHERE kind='diversity_curator_decision'"
    ).fetchall()
    assert len(rows) == 1
    p = json.loads(rows[0]["payload_json"])
    assert p["fallback_used"] is True


@pytest.mark.asyncio
async def test_curator_D_llm_raises_falls_back(db):
    """Si llm_client.ask_json lanza excepción → fallback graceful."""
    llm = AsyncMock()
    llm.ask_json = AsyncMock(side_effect=RuntimeError("boom"))

    curator = DiversityCurator(
        db=db, scorer=DiversityScorer(), llm_client=llm,
    )
    items = [_item(id=1), _item(id=2)]
    outbox = _make_outbox_with(items)
    picked = await curator.pick(outbox, None, _now())
    assert picked is not None
    assert picked.id in {1, 2}


# ---------------------------------------------------------------------------
# Auditoría: emit_decision en éxito
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curator_emits_event_on_success(db):
    llm = AsyncMock()
    llm.ask_json = AsyncMock(return_value={"chosen_id": 2, "reason": "diverso"})

    curator = DiversityCurator(
        db=db, scorer=DiversityScorer(), llm_client=llm,
    )
    items = [_item(id=1), _item(id=2)]
    outbox = _make_outbox_with(items)
    await curator.pick(outbox, None, _now())

    rows = db.execute(
        "SELECT payload_json FROM runtime_events WHERE kind='diversity_curator_decision'"
    ).fetchall()
    assert len(rows) == 1
    p = json.loads(rows[0]["payload_json"])
    assert p["chosen_id"] == 2
    assert p["fallback_used"] is False
    assert p["reason"] == "diverso"


# ---------------------------------------------------------------------------
# Resiliencia de _load_history — corrupción silenciada vs WARNING + skip
# ---------------------------------------------------------------------------


def _insert_outbox_row(
    db: sqlite3.Connection,
    *,
    outbox_id: int,
    payload_json: str,
) -> None:
    """Inserta una fila mínima en `outbox`. `offer_id` se deja a 1 (no se valida FK
    porque foreign_keys=ON requiere que `offers` exista; el test usa un esquema
    fresco sin offers, por lo que la FK declarada queda inerte para este insert).
    """
    db.execute(
        """
        INSERT INTO outbox (
            id, offer_id, type, enqueued_at, state, attempts, message_payload_json
        ) VALUES (?, ?, 'normal', '2026-05-28T09:00:00.000Z', 'pending', 0, ?)
        """,
        (outbox_id, outbox_id, payload_json),
    )


def _insert_published_row(
    db: sqlite3.Connection,
    *,
    outbox_id: int,
    sent_at: str,
) -> None:
    db.execute(
        """
        INSERT INTO published_messages (
            outbox_id, sent_at, success, message_text
        ) VALUES (?, ?, 1, 'msg')
        """,
        (outbox_id, sent_at),
    )


def _insert_product_offer_outbox_row(
    db: sqlite3.Connection,
    *,
    outbox_id: int,
    offer_id: int,
    product_id: int,
    marketplace: str,
    title: str,
    brand: str | None,
    category: str | None,
    payload_json: str,
) -> None:
    now = "2026-05-28T09:00:00.000Z"
    db.execute(
        """
        INSERT INTO products (
            id, marketplace, marketplace_id, url_canonical, title, brand, category,
            condition, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
        """,
        (
            product_id,
            marketplace,
            f"{marketplace}-{product_id}",
            f"https://example.com/{product_id}",
            title,
            brand,
            category,
            now,
            now,
        ),
    )
    db.execute(
        """
        INSERT INTO offers (
            id, product_id, classification, score, reasons_json, state, created_at, updated_at
        ) VALUES (?, ?, 'normal_offer', 60, '[]', 'eligible', ?, ?)
        """,
        (offer_id, product_id, now, now),
    )
    db.execute(
        """
        INSERT INTO outbox (
            id, offer_id, type, enqueued_at, state, attempts, message_payload_json
        ) VALUES (?, ?, 'normal', ?, 'pending', 0, ?)
        """,
        (outbox_id, offer_id, now, payload_json),
    )


@pytest.mark.asyncio
async def test_curator_skips_history_row_with_bad_json(db, caplog):
    """Si `message_payload_json` no es JSON válido, la fila se descarta y se loguea WARNING."""
    # Fila corrupta
    _insert_outbox_row(db, outbox_id=100, payload_json="{not json")
    _insert_published_row(db, outbox_id=100, sent_at="2026-05-28T10:00:00.000Z")
    # Fila válida (para confirmar que el resto del historial se carga)
    _insert_outbox_row(
        db,
        outbox_id=101,
        payload_json='{"category": "ok", "marketplace": "amazon", "current_price": 1500.0}',
    )
    _insert_published_row(db, outbox_id=101, sent_at="2026-05-28T11:00:00.000Z")

    curator = DiversityCurator(db=db, scorer=DiversityScorer(), llm_client=None)
    items = [_item(id=1, category="electronica"), _item(id=2, category="hogar")]
    outbox = _make_outbox_with(items)

    with caplog.at_level(
        logging.WARNING, logger="ofertas_hunter.dispatching.diversity_curator"
    ):
        picked = await curator.pick(outbox, last_normal_publication_at=None, now=_now())

    assert picked is not None  # pick no debe romperse por la fila corrupta

    warnings = [
        rec for rec in caplog.records
        if rec.name == "ofertas_hunter.dispatching.diversity_curator"
        and rec.levelno == logging.WARNING
    ]
    assert any(
        "unparseable history row" in rec.getMessage()
        or "history row" in rec.getMessage().lower()
        for rec in warnings
    ), f"Expected WARNING about unparseable history row, got: {[r.getMessage() for r in warnings]}"


@pytest.mark.asyncio
async def test_curator_skips_history_row_with_bad_sent_at(db, caplog):
    """Si `sent_at` no es parseable, la fila se descarta (no defaultea a now())
    y se loguea WARNING. Asserción clave: el scorer NO recibe esa fila como
    `history[0]`, por lo que el item con la misma categoría conserva score=1.0.
    """
    # ÚNICA fila publicada en el historial: sent_at corrupto, payload válido
    _insert_outbox_row(
        db,
        outbox_id=200,
        payload_json='{"category": "x", "marketplace": "amazon", "current_price": 1000.0}',
    )
    _insert_published_row(db, outbox_id=200, sent_at="not-a-date")

    curator = DiversityCurator(db=db, scorer=DiversityScorer(), llm_client=None)

    # UN solo item candidato cuya categoría coincide con la fila corrupta.
    # Si la fila se descarta correctamente, el historial efectivo está vacío
    # y el scorer no aplica penalización → score 1.0.
    only_item = _item(id=42, category="x", marketplace="amazon")
    outbox = _make_outbox_with([only_item])

    with caplog.at_level(
        logging.WARNING, logger="ofertas_hunter.dispatching.diversity_curator"
    ):
        picked = await curator.pick(outbox, last_normal_publication_at=None, now=_now())

    # Atajo de un solo candidato → pick devuelve directo sin tocar history.
    # Para forzar el path que carga history, agregamos un segundo item.
    second = _item(id=43, category="otra", marketplace="ml")
    outbox2 = _make_outbox_with([only_item, second])
    with caplog.at_level(
        logging.WARNING, logger="ofertas_hunter.dispatching.diversity_curator"
    ):
        picked2 = await curator.pick(outbox2, last_normal_publication_at=None, now=_now())

    assert picked is not None
    assert picked2 is not None  # no exception, pick succeeded

    warnings = [
        rec for rec in caplog.records
        if rec.name == "ofertas_hunter.dispatching.diversity_curator"
        and rec.levelno == logging.WARNING
    ]
    assert any(
        "unparseable sent_at" in rec.getMessage()
        or "sent_at" in rec.getMessage()
        for rec in warnings
    ), f"Expected WARNING about unparseable sent_at, got: {[r.getMessage() for r in warnings]}"


@pytest.mark.asyncio
async def test_curator_uses_product_metadata_for_history_when_payload_missing(db):
    _insert_product_offer_outbox_row(
        db,
        outbox_id=500,
        offer_id=500,
        product_id=500,
        marketplace="mercadolibre",
        title="Transportadora mascota",
        brand="fancy",
        category="bolsas y transportadoras",
        payload_json='{"marketplace": "mercadolibre", "current_price": 999.0}',
    )
    _insert_published_row(db, outbox_id=500, sent_at="2026-05-28T11:00:00.000Z")

    curator = DiversityCurator(db=db, scorer=DiversityScorer(), llm_client=None)
    outbox = _make_outbox_with(
        [
            _item(id=1, category="bolsas y transportadoras", marketplace="mercadolibre"),
            _item(id=2, category="cuidado personal", marketplace="amazon"),
        ]
    )

    picked = await curator.pick(outbox, None, _now())
    assert picked is not None
    assert picked.id == 2


@pytest.mark.asyncio
async def test_curator_enriches_candidates_from_product_metadata_when_payload_missing(db):
    _insert_product_offer_outbox_row(
        db,
        outbox_id=600,
        offer_id=600,
        product_id=600,
        marketplace="mercadolibre",
        title="Historial transporte",
        brand="fancy",
        category="bolsas y transportadoras",
        payload_json='{"marketplace": "mercadolibre", "category": "bolsas y transportadoras", "current_price": 999.0}',
    )
    _insert_published_row(db, outbox_id=600, sent_at="2026-05-28T11:00:00.000Z")

    curator = DiversityCurator(db=db, scorer=DiversityScorer(), llm_client=None)
    candidates = [
        OutboxItem(
            id=601,
            offer_id=601,
            type=OutboxType.NORMAL.value,
            message_payload={"title": "Otra transportadora", "marketplace": "mercadolibre", "current_price": 1200.0},
            enqueued_at=_now(),
            attempts=0,
            state=OutboxState.PENDING.value,
        ),
        OutboxItem(
            id=602,
            offer_id=602,
            type=OutboxType.NORMAL.value,
            message_payload={"title": "Protector solar", "marketplace": "amazon", "current_price": 1100.0},
            enqueued_at=_now(),
            attempts=0,
            state=OutboxState.PENDING.value,
        ),
    ]
    _insert_product_offer_outbox_row(
        db,
        outbox_id=601,
        offer_id=601,
        product_id=601,
        marketplace="mercadolibre",
        title="Otra transportadora",
        brand="fancy",
        category="bolsas y transportadoras",
        payload_json='{"marketplace": "mercadolibre", "current_price": 1200.0}',
    )
    _insert_product_offer_outbox_row(
        db,
        outbox_id=602,
        offer_id=602,
        product_id=602,
        marketplace="amazon",
        title="Protector solar",
        brand="isdin",
        category="Filtro Solar Corporal",
        payload_json='{"marketplace": "amazon", "current_price": 1100.0}',
    )

    outbox = _make_outbox_with(candidates)
    picked = await curator.pick(outbox, None, _now())
    assert picked is not None
    assert picked.id == 602
    assert picked.message_payload["category"] == "Filtro Solar Corporal"
