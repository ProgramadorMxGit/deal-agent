"""Tests del planner de déficit + promoción de deferred. Criterios A,B,H,I,M,Q."""
from __future__ import annotations

import json
import sqlite3

import pytest

from ofertas_hunter.db import init_db
from ofertas_hunter.dispatching.outbox_admission import QuotaConfig, load_pending_snapshot
from ofertas_hunter.dispatching.category_deficit_planner import (
    PlannerConfig, compute_plan, maybe_emit_plan, promote_deferred,
    EVENT_PLAN, EVENT_PROMOTED,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    init_db(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _enq(db, *, oid, title, mkt, brand, state="pending", discount=55.0, deferred_at=None):
    payload = {"title": title, "marketplace": mkt, "brand": brand,
               "discount_percent": discount, "item_id": f"ID{oid}"}
    if deferred_at:
        payload["deferred_at"] = deferred_at
        payload["defer_reason"] = "category_quota_saturated"
    db.execute(
        "INSERT INTO outbox (offer_id, type, enqueued_at, scheduled_for, attempts, "
        "last_attempt_at, state, message_payload_json) "
        "VALUES (?, 'normal', '2026-05-30T20:00:00.000Z', NULL, 0, NULL, ?, ?)",
        (oid, state, json.dumps(payload, ensure_ascii=False)),
    )
    db.commit()


def _cfg(**kw):
    base = dict(max_category_pct=40.0, max_brand_pct=20.0, max_marketplace_pct=75.0,
                defer_max_minutes=240, min_pending_before_quota=8)
    base.update(kw)
    return QuotaConfig(**base)


def _planner():
    return PlannerConfig(enabled=True, plan_every_seconds=300)


# A — belleza 80% detectada saturada
def test_A_detects_saturated_belleza(db):
    for i in range(8):
        _enq(db, oid=i + 1, title=f"Protector Solar Facial var{i}", mkt="amazon", brand=f"b{i}")
    for i in range(2):
        _enq(db, oid=100 + i, title=f"Laptop HP var{i}", mkt="amazon", brand=f"hp{i}")
    plan = compute_plan(db, _planner(), _cfg())
    assert "belleza" in plan["saturated_categories"]


# B — tecnologia/hogar/bebe deficitarias
def test_B_detects_deficit_categories(db):
    for i in range(10):
        _enq(db, oid=i + 1, title=f"Protector Solar Facial var{i}", mkt="amazon", brand=f"b{i}")
    plan = compute_plan(db, _planner(), _cfg())
    for c in ("tecnologia", "hogar", "bebe", "herramientas"):
        assert c in plan["deficit_categories"]
    assert plan["recommended_frontier_categories"]


# Q — maybe_emit_plan registra runtime_event
def test_Q_emits_plan_event(db):
    for i in range(10):
        _enq(db, oid=i + 1, title=f"Protector Solar var{i}", mkt="amazon", brand=f"b{i}")
    plan = maybe_emit_plan(db, _planner(), _cfg())
    assert plan is not None
    rows = db.execute("SELECT COUNT(*) n FROM runtime_events WHERE kind=?", (EVENT_PLAN,)).fetchone()
    assert rows["n"] == 1
    # segundo llamado inmediato no duplica (rate limit)
    maybe_emit_plan(db, _planner(), _cfg())
    rows = db.execute("SELECT COUNT(*) n FROM runtime_events WHERE kind=?", (EVENT_PLAN,)).fetchone()
    assert rows["n"] == 1


# H — deferred se promueve cuando categoría/marca bajan del cupo
def test_H_promotes_when_not_saturated(db):
    import datetime as _dt
    recent = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    # pending grande y variado: 6 items de marcas/categorias distintas, de modo
    # que un belleza mas (marca unica) NO satura categoria (1/7=14%) ni marca (1/7=14%).
    _enq(db, oid=1, title="Laptop HP Core i5", mkt="amazon", brand="hp")
    _enq(db, oid=2, title="Licuadora Oster", mkt="mercadolibre", brand="oster")
    _enq(db, oid=4, title="Taladro Bosch", mkt="mercadolibre", brand="bosch")
    _enq(db, oid=5, title="Tenis Nike Running", mkt="mercadolibre", brand="nike")
    _enq(db, oid=6, title="Sarten Vasconia", mkt="amazon", brand="vasconia")
    _enq(db, oid=7, title="Mochila Bebe Carriola", mkt="mercadolibre", brand="chicco")
    # deferred de belleza reciente con marca unica -> debe promover NORMAL
    _enq(db, oid=3, title="Protector Solar MarcaUnica FPS", mkt="amazon", brand="marcaunica",
         state="deferred", deferred_at=recent)
    res = promote_deferred(db, _cfg(min_pending_before_quota=0), limit=10)
    assert (res["promoted"] + res["promoted_degraded"]) >= 1
    row = db.execute("SELECT state FROM outbox WHERE offer_id=3").fetchone()
    assert row["state"] == "pending"


# I — deferred se promueve degradado tras max minutos aunque saturado
def test_I_promotes_degraded_when_aged_out(db):
    # pool saturado de belleza
    for i in range(10):
        _enq(db, oid=i + 1, title=f"Protector Solar var{i}", mkt="amazon", brand=f"b{i}")
    # deferred de belleza viejo (deferred_at hace mucho)
    _enq(db, oid=200, title="Protector Solar Viejo", mkt="amazon", brand="nivea",
         state="deferred", deferred_at="2020-01-01T00:00:00.000Z")
    res = promote_deferred(db, _cfg(defer_max_minutes=1, min_pending_before_quota=8), limit=10)
    assert res["promoted_degraded"] >= 1
    row = db.execute("SELECT state, message_payload_json FROM outbox WHERE offer_id=200").fetchone()
    assert row["state"] == "pending"
    p = json.loads(row["message_payload_json"])
    assert p.get("promoted_degraded") is True


# M — promoción no toca sent/discarded
def test_M_does_not_touch_sent_discarded(db):
    _enq(db, oid=1, title="Whey BHP", mkt="mercadolibre", brand="bhp", state="sent")
    _enq(db, oid=2, title="Crema Vieja", mkt="amazon", brand="x", state="discarded")
    _enq(db, oid=3, title="Laptop HP", mkt="amazon", brand="hp", state="pending")
    promote_deferred(db, _cfg(min_pending_before_quota=0), limit=10)
    assert db.execute("SELECT state FROM outbox WHERE offer_id=1").fetchone()["state"] == "sent"
    assert db.execute("SELECT state FROM outbox WHERE offer_id=2").fetchone()["state"] == "discarded"
