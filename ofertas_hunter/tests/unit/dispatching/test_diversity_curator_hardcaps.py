"""Tests del DiversityCurator con topes duros (hard caps), override y traza.

Cubre criterios del spec:
F. ventana con 3 proteínas bloquea 4ª proteína si hay alternativa (elige alt).
G. ventana con 2 BHP bloquea 3ª BHP si hay alternativa.
I. todos violan diversidad + override on → elige menos malo con override reason.
J. override off → no publica.
K. selector persiste evento con rejected_candidates y reasons.
L. LLM failure cae a fallback sin romper.
Integración: pool 10 proteínas ML + 2 tech → tras 3 proteínas elige tech.
            variantes del mismo producto → solo una en ventana.
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
from ofertas_hunter.dispatching.diversity_filters import HardCapConfig
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
    return datetime(2026, 5, 30, 22, 0, 0, tzinfo=timezone.utc)


def _make_outbox(items):
    ob = InMemoryOutbox(OutboxConfig(cooldown=CooldownPolicy(0)))
    for it in items:
        ob.enqueue(it)
    return ob


def _item(id, title, *, mkt="mercadolibre", item_id=None, brand=None, price=800.0):
    payload = {
        "title": title,
        "marketplace": mkt,
        "item_id": item_id or f"ID{id}",
        "current_price": price,
        "discount_percent": 55.0,
        "image_url": "https://x/img",
        "url": "https://x",
    }
    if brand:
        payload["brand"] = brand
    return OutboxItem(
        id=id, offer_id=id, type=OutboxType.NORMAL.value,
        message_payload=payload, enqueued_at=_now(), state=OutboxState.PENDING.value,
    )


def _seed_history(db, rows):
    """rows: list of (outbox_id, sent_at, payload_dict)."""
    for outbox_id, sent_at, payload in rows:
        db.execute(
            "INSERT INTO outbox (id, offer_id, type, enqueued_at, state, attempts, message_payload_json) "
            "VALUES (?, ?, 'normal', '2026-05-30T20:00:00.000Z', 'sent', 1, ?)",
            (outbox_id, outbox_id, json.dumps(payload, ensure_ascii=False)),
        )
        db.execute(
            "INSERT INTO published_messages (outbox_id, sent_at, success, message_text) "
            "VALUES (?, ?, 1, 'm')",
            (outbox_id, sent_at),
        )
    db.commit()


def _curator(db, *, llm=None, cfg=None, use_trace=True):
    return DiversityCurator(
        db=db, scorer=DiversityScorer(), llm_client=llm,
        hard_cap_config=cfg or HardCapConfig(),
        trace_decisions=use_trace,
    )


# F — tras 3 proteínas, elige la alternativa tech
@pytest.mark.asyncio
async def test_F_after_three_protein_picks_tech(db):
    hist = [
        (10, "2026-05-30T21:40:00.000Z", {"title": "BHP Whey Vainilla", "marketplace": "mercadolibre", "brand": "bhp"}),
        (11, "2026-05-30T21:45:00.000Z", {"title": "43 Whey Galleta", "marketplace": "mercadolibre", "brand": "43"}),
        (12, "2026-05-30T21:50:00.000Z", {"title": "Empower Whey Chocolate", "marketplace": "mercadolibre", "brand": "empower"}),
    ]
    _seed_history(db, hist)
    cur = _curator(db)
    items = [
        _item(1, "Proteina Unique Whey 2.2 Kg Fresa", brand="unique"),
        _item(2, "Laptop HP Core i5 16GB SSD", mkt="amazon", brand="hp"),
    ]
    picked = await cur.pick(_make_outbox(items), None, _now())
    assert picked is not None
    assert picked.id == 2  # la proteína (4ª) está bloqueada por categoría


# G — 2 BHP en historial bloquea 3ª BHP
@pytest.mark.asyncio
async def test_G_two_bhp_blocks_third_bhp(db):
    hist = [
        (10, "2026-05-30T21:50:00.000Z", {"title": "BHP Whey Vainilla", "marketplace": "mercadolibre", "brand": "bhp"}),
        (11, "2026-05-30T21:55:00.000Z", {"title": "BHP Iso Protein Ponche", "marketplace": "mercadolibre", "brand": "bhp"}),
    ]
    _seed_history(db, hist)
    # subir el cap de categoría para aislar el de marca
    cfg = HardCapConfig(max_same_category=9, max_same_brand=2, max_same_product_family=9)
    cur = _curator(db, cfg=cfg)
    items = [
        _item(1, "BHP Just Whey Natural 500g", brand="bhp"),       # 3ª BHP -> bloqueada
        _item(2, "Optimum Gold Standard Whey", brand="optimum"),
    ]
    picked = await cur.pick(_make_outbox(items), None, _now())
    assert picked is not None
    assert picked.id == 2


# I — todos violan + override on -> elige menos malo
@pytest.mark.asyncio
async def test_I_override_picks_least_bad(db):
    hist = [
        (10, "2026-05-30T21:40:00.000Z", {"title": "BHP Whey Protein Vainilla", "marketplace": "mercadolibre", "brand": "bhp"}),
        (11, "2026-05-30T21:45:00.000Z", {"title": "43 Whey Protein Ponche", "marketplace": "mercadolibre", "brand": "43"}),
        (12, "2026-05-30T21:50:00.000Z", {"title": "Empower Whey Protein Galleta", "marketplace": "mercadolibre", "brand": "empower"}),
    ]
    _seed_history(db, hist)
    cfg = HardCapConfig(allow_override_if_no_alternative=True)
    cur = _curator(db, cfg=cfg)
    # ambos proteína (categoría ya en 3) → todos violan
    items = [
        _item(1, "BHP Ultra Whey Protein Chocolate", brand="bhp"),
        _item(2, "Optimum Gold Whey Protein Vanilla", brand="optimum"),
    ]
    picked = await cur.pick(_make_outbox(items), None, _now())
    assert picked is not None  # override publica el menos malo
    rows = db.execute(
        "SELECT payload_json FROM runtime_events WHERE kind='diversity_curator_decision' ORDER BY id DESC LIMIT 1"
    ).fetchall()
    p = json.loads(rows[0]["payload_json"])
    assert p["override_used"] is True
    assert p["override_reason"] == "diversity_override_no_alternative"


# J — override off -> no publica
@pytest.mark.asyncio
async def test_J_override_off_does_not_publish(db):
    hist = [
        (10, "2026-05-30T21:40:00.000Z", {"title": "BHP Whey Protein Vainilla", "marketplace": "mercadolibre", "brand": "bhp"}),
        (11, "2026-05-30T21:45:00.000Z", {"title": "43 Whey Protein Ponche", "marketplace": "mercadolibre", "brand": "43"}),
        (12, "2026-05-30T21:50:00.000Z", {"title": "Empower Whey Protein Galleta", "marketplace": "mercadolibre", "brand": "empower"}),
    ]
    _seed_history(db, hist)
    cfg = HardCapConfig(allow_override_if_no_alternative=False)
    cur = _curator(db, cfg=cfg)
    items = [
        _item(1, "BHP Ultra Whey Protein Chocolate", brand="bhp"),
        _item(2, "43 Zero Hidrolizada Whey Platano", brand="43"),
    ]
    picked = await cur.pick(_make_outbox(items), None, _now())
    assert picked is None
    rows = db.execute(
        "SELECT payload_json FROM runtime_events WHERE kind='diversity_curator_decision' ORDER BY id DESC LIMIT 1"
    ).fetchall()
    p = json.loads(rows[0]["payload_json"])
    assert p["reason"] == "no_publish_diversity_blocked"


# K — persiste rejected_candidates con reasons
@pytest.mark.asyncio
async def test_K_persists_rejected_candidates(db):
    hist = [
        (10, "2026-05-30T21:40:00.000Z", {"title": "BHP Whey Vainilla", "marketplace": "mercadolibre", "brand": "bhp"}),
        (11, "2026-05-30T21:45:00.000Z", {"title": "BHP Iso Ponche", "marketplace": "mercadolibre", "brand": "bhp"}),
        (12, "2026-05-30T21:50:00.000Z", {"title": "43 Whey Galleta", "marketplace": "mercadolibre", "brand": "43"}),
    ]
    _seed_history(db, hist)
    cur = _curator(db)
    items = [
        _item(1, "BHP Ultra Whey Chocolate", brand="bhp"),    # proteína -> rechazada
        _item(2, "Laptop HP Core i5", mkt="amazon", brand="hp"),  # tech -> kept
    ]
    picked = await cur.pick(_make_outbox(items), None, _now())
    assert picked.id == 2
    rows = db.execute(
        "SELECT payload_json FROM runtime_events WHERE kind='diversity_curator_decision' ORDER BY id DESC LIMIT 1"
    ).fetchall()
    p = json.loads(rows[0]["payload_json"])
    assert p["candidates_count"] == 2
    assert p["candidates_after_hard_caps"] == 1
    assert any(r["outbox_id"] == 1 for r in p["rejected_candidates"])
    assert p["selected_category_normalized"] == "tecnologia"
    assert "window_stats" in p


# L — LLM failure cae a fallback sin romper
@pytest.mark.asyncio
async def test_L_llm_failure_falls_back(db):
    llm = AsyncMock()
    llm.ask_json = AsyncMock(side_effect=RuntimeError("boom"))
    cur = _curator(db, llm=llm)
    items = [
        _item(1, "Laptop HP Core i5", mkt="amazon", brand="hp"),
        _item(2, "Robot Aspirador Bluelander", mkt="amazon", brand="bluelander"),
    ]
    picked = await cur.pick(_make_outbox(items), None, _now())
    assert picked is not None
    rows = db.execute(
        "SELECT payload_json FROM runtime_events WHERE kind='diversity_curator_decision' ORDER BY id DESC LIMIT 1"
    ).fetchall()
    p = json.loads(rows[0]["payload_json"])
    assert p["fallback_used"] is True
    assert p["llm_used"] is True


# Integración — pool 10 proteínas ML + 2 tech, tras 3 proteínas elige tech
@pytest.mark.asyncio
async def test_integration_mixed_pool_prefers_tech_after_protein_streak(db):
    hist = [
        (10, "2026-05-30T21:40:00.000Z", {"title": "BHP Whey Vainilla", "marketplace": "mercadolibre", "brand": "bhp"}),
        (11, "2026-05-30T21:45:00.000Z", {"title": "43 Whey Galleta", "marketplace": "mercadolibre", "brand": "43"}),
        (12, "2026-05-30T21:50:00.000Z", {"title": "Empower Whey Chocolate", "marketplace": "mercadolibre", "brand": "empower"}),
    ]
    _seed_history(db, hist)
    cur = _curator(db)
    proteins = [_item(100 + i, f"Proteina Marca{i} Whey {i}kg", brand=f"marca{i}") for i in range(10)]
    techs = [
        _item(200, "Laptop Lenovo Ideapad Ryzen 5", mkt="amazon", brand="lenovo"),
        _item(201, "Monitor LG 27 Pulgadas IPS", mkt="amazon", brand="lg"),
    ]
    picked = await cur.pick(_make_outbox(proteins + techs), None, _now())
    assert picked is not None
    assert picked.id in (200, 201)  # debe elegir tech, no la 4ª proteína


# Integración — variantes del mismo producto: solo una publicable en ventana
@pytest.mark.asyncio
async def test_integration_variants_only_one_in_window(db):
    # historial ya tiene una variante BHP Ultra Whey
    hist = [
        (10, "2026-05-30T21:55:00.000Z",
         {"title": "Proteina Bhp Ultra Whey Ultra 2.27 Kg 5 Lbs Bote Sabor Vainilla",
          "marketplace": "mercadolibre", "brand": "bhp"}),
    ]
    _seed_history(db, hist)
    cfg = HardCapConfig(max_same_category=9, max_same_brand=9, max_same_product_family=1)
    cur = _curator(db, cfg=cfg)
    items = [
        _item(1, "Proteina Bhp Ultra Whey Ultra 2.27 Kg 5 Lbs Bote Chocolate", brand="bhp"),  # variante -> bloqueada
        _item(2, "Tenis Nike Running Negro", mkt="amazon", brand="nike"),
    ]
    picked = await cur.pick(_make_outbox(items), None, _now())
    assert picked.id == 2
