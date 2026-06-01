"""Tests de cuotas de entrada al pending (Opción A). Criterios C-G, O.

C) categoría >40% -> deferred.
D) marca >20% -> deferred.
E) marketplace >75% -> deferred (con alternativa).
F) descuento >=70 -> pending por excepción.
G) deferred no se descarta ni se marca sent.
O) (gate de descuento es responsabilidad del hunter; aquí no se valida <50,
   pero confirmamos que la admisión no toca el descuento).
P) deferred conserva trazabilidad.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from ofertas_hunter.db import init_db
from ofertas_hunter.dispatching.outbox_admission import (
    QuotaConfig, decide_admission, enqueue_with_quota, load_pending_snapshot,
    DEFER_REASON_CATEGORY, DEFER_REASON_BRAND, DEFER_REASON_MARKETPLACE,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    init_db(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _enqueue_pending(db, *, offer_id, title, marketplace, brand=None, discount=55.0, state="pending"):
    payload = {
        "title": title, "marketplace": marketplace, "brand": brand,
        "discount_percent": discount, "item_id": f"ID{offer_id}",
    }
    db.execute(
        "INSERT INTO outbox (offer_id, type, enqueued_at, scheduled_for, attempts, "
        "last_attempt_at, state, message_payload_json) "
        "VALUES (?, 'normal', '2026-05-30T20:00:00.000Z', NULL, 0, NULL, ?, ?)",
        (offer_id, state, json.dumps(payload, ensure_ascii=False)),
    )
    db.commit()


def _cfg(**kw):
    base = dict(enabled=True, max_category_pct=40.0, max_brand_pct=20.0,
                max_marketplace_pct=75.0, defer_minutes=60, defer_max_minutes=240,
                exceptional_discount_threshold=70.0, min_pending_before_quota=8)
    base.update(kw)
    return QuotaConfig(**base)


def _fill_belleza(db, n=10):
    # n protectores solares Amazon de marcas variadas
    brands = ["nivea", "vichy", "cetaphil", "garnier", "eucerin", "avene",
              "loreal", "neutrogena", "isdin", "hawaiian"]
    for i in range(n):
        _enqueue_pending(db, offer_id=i + 1,
                         title=f"Protector Solar {brands[i % len(brands)]} FPS 50 var{i}",
                         marketplace="amazon", brand=brands[i % len(brands)])


# C — categoría saturada -> deferred
def test_C_category_saturated_defers(db):
    _fill_belleza(db, 10)  # 100% belleza
    payload = {"title": "Crema Facial Otra Marca Belleza", "marketplace": "amazon",
               "brand": "ponds", "discount_percent": 55.0}
    d = decide_admission(db, payload, _cfg())
    assert d.state == "deferred"
    assert d.defer_reason == DEFER_REASON_CATEGORY


# D — marca saturada -> deferred (categoria no saturada)
def test_D_brand_saturated_defers(db):
    # pool variado en categoría pero con muchas ISDIN
    for i in range(10):
        cat = "tecnologia" if i % 2 else "hogar"
        title = ("Laptop HP" if i % 2 else "Licuadora Oster") + f" var{i}"
        _enqueue_pending(db, offer_id=i + 1, title=title, marketplace="amazon",
                         brand="isdin")  # marca isdin en todos -> 100% marca
    payload = {"title": "Sarten Teflon Cocina", "marketplace": "amazon",
               "brand": "isdin", "discount_percent": 55.0}
    d = decide_admission(db, payload, _cfg(max_category_pct=99.0))
    assert d.state == "deferred"
    assert d.defer_reason == DEFER_REASON_BRAND


# E — marketplace saturado -> deferred (hay alternativa de otro mkt)
def test_E_marketplace_saturated_defers(db):
    for i in range(9):
        _enqueue_pending(db, offer_id=i + 1, title=f"Laptop HP var{i}",
                         marketplace="amazon", brand=f"hp{i}")
    _enqueue_pending(db, offer_id=99, title="Proteina Whey ML", marketplace="mercadolibre", brand="bhp")
    payload = {"title": "Monitor LG 27", "marketplace": "amazon",
               "brand": "lg", "discount_percent": 55.0}
    d = decide_admission(db, payload, _cfg(max_category_pct=99.0, max_brand_pct=99.0))
    assert d.state == "deferred"
    assert d.defer_reason == DEFER_REASON_MARKETPLACE


# F — descuento excepcional entra pending aunque saturado
def test_F_exceptional_discount_enters_pending(db):
    _fill_belleza(db, 10)
    payload = {"title": "Protector Solar Premium", "marketplace": "amazon",
               "brand": "nivea", "discount_percent": 75.0}
    d = decide_admission(db, payload, _cfg())
    assert d.state == "pending"
    assert d.extra_payload.get("quota_override") == "quota_override_exceptional_discount"


# G + P — deferred via enqueue_with_quota: no sent/discarded, conserva trazabilidad
def test_G_P_deferred_state_and_traceability(db):
    _fill_belleza(db, 10)
    payload = {"title": "Otra Crema Belleza Facial", "marketplace": "amazon",
               "brand": "ponds", "discount_percent": 55.0, "item_id": "NEW1"}
    oid = enqueue_with_quota(db, offer_id=500, outbox_type="normal", payload=payload, config=_cfg())
    db.commit()
    row = db.execute("SELECT state, scheduled_for, message_payload_json FROM outbox WHERE id=?", (oid,)).fetchone()
    assert row["state"] == "deferred"
    assert row["scheduled_for"] is not None
    p = json.loads(row["message_payload_json"])
    assert p["defer_reason"] == DEFER_REASON_CATEGORY
    assert "deferred_at" in p and "quota_snapshot" in p
    assert p["promotion_attempts"] == 0
    # no debe haberse marcado sent ni discarded
    assert row["state"] not in ("sent", "discarded")


# pending pequeño -> no difiere (mantener material)
def test_small_pending_does_not_defer(db):
    _fill_belleza(db, 3)  # < min_pending_before_quota=8
    payload = {"title": "Protector Solar Otra", "marketplace": "amazon",
               "brand": "nivea", "discount_percent": 55.0}
    d = decide_admission(db, payload, _cfg())
    assert d.state == "pending"


# categoría deficitaria entra pending aunque pool saturado de belleza
def test_deficit_category_enters_pending(db):
    _fill_belleza(db, 10)
    payload = {"title": "Laptop HP Core i5 16GB SSD", "marketplace": "amazon",
               "brand": "hp", "discount_percent": 55.0}
    d = decide_admission(db, payload, _cfg())
    assert d.state == "pending"  # tecnologia no saturada


# disabled -> siempre pending
def test_disabled_quota_always_pending(db):
    _fill_belleza(db, 10)
    payload = {"title": "Protector Solar Otra", "marketplace": "amazon",
               "brand": "nivea", "discount_percent": 55.0}
    d = decide_admission(db, payload, _cfg(enabled=False))
    assert d.state == "pending"
