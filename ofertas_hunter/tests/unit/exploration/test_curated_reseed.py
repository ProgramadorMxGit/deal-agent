"""Tests de reseed_curated + decay_old_backlog (FASE 4/5/11)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ofertas_hunter.db import init_db
from ofertas_hunter.exploration.curated_reseed import (
    reseed_curated, decay_old_backlog, EVENT_RESEED, EVENT_REBALANCE,
)


@pytest.fixture
def db(tmp_path):
    init_db(tmp_path / "t.db")
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def repo(tmp_path):
    seeds = tmp_path / "config" / "seeds"
    seeds.mkdir(parents=True)
    (seeds / "mercadolibre.json").write_text(json.dumps([
        "https://www.mercadolibre.com.mx/ofertas/cocina",
        "https://listado.mercadolibre.com.mx/olla_Descuento_50-100",
        "https://www.mercadolibre.com.mx/c/computacion",  # NO curada
    ]), encoding="utf-8")
    (seeds / "amazon.json").write_text(json.dumps([
        "https://www.amazon.com.mx/deals?bubble-id=deals-collection-home-kitchen",
        "https://www.amazon.com.mx/gp/bestsellers/",  # NO curada
    ]), encoding="utf-8")
    return tmp_path


def test_reseed_adds_curated_seeds(db, repo):
    res = reseed_curated(db, repo_root=repo, marketplaces=["amazon", "mercadolibre"],
                         min_score=20.0, every_minutes=60)
    assert res["reseeded"] >= 2  # 2 ML curadas + 1 Amazon curada
    rows = db.execute("SELECT url_canonical, score FROM frontier").fetchall()
    urls = {r["url_canonical"] for r in rows}
    assert any("ofertas/cocina" in u for u in urls)
    assert any("deals-collection" in u for u in urls)
    # las NO curadas no se reinyectan
    assert not any("/c/computacion" in u for u in urls)
    assert not any("bestsellers" in u for u in urls)
    # todas con score alto
    assert all(r["score"] == 20.0 for r in rows)


def test_reseed_rate_limited(db, repo):
    reseed_curated(db, repo_root=repo, marketplaces=["mercadolibre"], every_minutes=60)
    # segunda llamada inmediata -> rate limited
    res2 = reseed_curated(db, repo_root=repo, marketplaces=["mercadolibre"], every_minutes=60)
    assert res2.get("skipped") == "rate_limited"


def test_reseed_emits_event(db, repo):
    reseed_curated(db, repo_root=repo, marketplaces=["amazon", "mercadolibre"], every_minutes=60)
    n = db.execute("SELECT COUNT(*) n FROM runtime_events WHERE kind=?", (EVENT_RESEED,)).fetchone()["n"]
    assert n == 1


def test_reseed_bumps_existing_low_score(db, repo):
    # insertar la seed con score bajo
    db.execute(
        "INSERT INTO frontier (marketplace, url_canonical, url_type, score, added_at, retries) "
        "VALUES ('mercadolibre','https://www.mercadolibre.com.mx/ofertas/cocina','deals',1.0,'2026-05-30T00:00:00Z',0)"
    )
    db.commit()
    res = reseed_curated(db, repo_root=repo, marketplaces=["mercadolibre"], min_score=20.0, every_minutes=60)
    assert res["bumped"] >= 1
    score = db.execute("SELECT score FROM frontier WHERE url_canonical LIKE '%ofertas/cocina%'").fetchone()["score"]
    assert score == 20.0


def test_decay_lowers_old_non_curated(db):
    # url vieja no curada con score alto
    db.execute(
        "INSERT INTO frontier (marketplace, url_canonical, url_type, score, added_at, retries) "
        "VALUES ('mercadolibre','https://articulo.mercadolibre.com.mx/MLM111','product',10.0,'2020-01-01T00:00:00Z',0)"
    )
    # url vieja CURADA -> no debe decaer
    db.execute(
        "INSERT INTO frontier (marketplace, url_canonical, url_type, score, added_at, retries) "
        "VALUES ('mercadolibre','https://www.mercadolibre.com.mx/ofertas/cocina','deals',20.0,'2020-01-01T00:00:00Z',0)"
    )
    db.commit()
    res = decay_old_backlog(db, decay_hours=24, decay_factor=0.25)
    assert res["decayed"] >= 1
    old = db.execute("SELECT score FROM frontier WHERE url_canonical LIKE '%MLM111%'").fetchone()["score"]
    curated = db.execute("SELECT score FROM frontier WHERE url_canonical LIKE '%ofertas/cocina%'").fetchone()["score"]
    assert old == 2.5  # 10 * 0.25
    assert curated == 20.0  # curada intacta
    ev = db.execute("SELECT COUNT(*) n FROM runtime_events WHERE kind=?", (EVENT_REBALANCE,)).fetchone()["n"]
    assert ev >= 1
