"""Tests del DiversityCurator — orquestación scorer + LLM + fallback.

Cubre criterios A-G del spec:
A. Solo determinístico (use_llm=false) → publica top1 score.
B. LLM elige válido → publica el id elegido.
C. LLM elige inválido (id fuera de top10) → fallback a top1.
D. LLM timeout → fallback a top1.
E. LLM no instalado → arranca y degrada a top1.
F. Sin candidatos → retorna None.
G. Un solo candidato → skip LLM, retorna directo.
"""
from __future__ import annotations

import json
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
