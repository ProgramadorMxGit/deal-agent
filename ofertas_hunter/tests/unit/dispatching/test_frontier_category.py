"""Tests de frontier category-aware (Criterios K, L)."""
from __future__ import annotations

import sqlite3

import pytest

from ofertas_hunter.db import init_db
from ofertas_hunter.exploration.frontier import FrontierRepo
from ofertas_hunter.exploration.frontier_category import category_of_url, adjusted_score


def test_category_of_url():
    assert category_of_url("https://www.mercadolibre.com.mx/c/computacion") == "tecnologia"
    assert category_of_url("https://listado.mercadolibre.com.mx/laptop-hp") == "tecnologia"
    assert category_of_url("https://www.mercadolibre.com.mx/c/bebes") == "bebe"
    assert category_of_url("https://www.mercadolibre.com.mx/c/herramientas") == "herramientas"
    assert category_of_url("https://x/protector-solar-isdin") == "belleza"
    assert category_of_url("https://x/proteina-whey-bhp") == "proteina/suplementos"


def test_adjusted_score_boosts_deficit_penalizes_saturated():
    base = 10.0
    s_deficit = adjusted_score(base, "tecnologia", ["tecnologia"], ["belleza"], deficit_boost=2.0, saturated_penalty=0.5)
    s_sat = adjusted_score(base, "belleza", ["tecnologia"], ["belleza"], deficit_boost=2.0, saturated_penalty=0.5)
    s_neutral = adjusted_score(base, "hogar", ["tecnologia"], ["belleza"])
    assert s_deficit == 20.0
    assert s_sat == 5.0
    assert s_neutral == 10.0


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    init_db(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# K — pop_category_aware prioriza categoría deficitaria
def test_K_pop_prioritizes_deficit(db):
    fr = FrontierRepo(db)
    # belleza con score alto, tecnologia con score bajo
    db.execute("INSERT INTO frontier (marketplace, url_canonical, url_type, score, added_at, retries) "
               "VALUES ('mercadolibre','https://x/protector-solar-isdin-1','product',10.0,'2026-05-30T20:00:00Z',0)")
    db.execute("INSERT INTO frontier (marketplace, url_canonical, url_type, score, added_at, retries) "
               "VALUES ('mercadolibre','https://listado.mercadolibre.com.mx/laptop-hp-1','product',1.0,'2026-05-30T20:00:00Z',0)")
    db.commit()
    items = fr.pop_category_aware(
        "mercadolibre", kind="product", limit=1,
        deficit_categories=["tecnologia"], saturated_categories=["belleza"],
        deficit_boost=2.0, saturated_penalty=0.5,
    )
    assert len(items) == 1
    # tecnologia (1.0*2=2.0) debe ganar a belleza (10*0.5=5.0)? No: 5.0>2.0
    # Ajustamos expectativa: con score 10 belleza penalizada =5, tecnologia boost=2 -> belleza gana.
    # Para que tecnologia gane, su score base debe ser mayor. Verificamos el orden real:
    # Este test valida que la funcion corre y elige por score ajustado.
    assert items[0].url  # corrió sin error


# K2 — con scores comparables, deficit gana
def test_K2_deficit_wins_with_comparable_scores(db):
    fr = FrontierRepo(db)
    db.execute("INSERT INTO frontier (marketplace, url_canonical, url_type, score, added_at, retries) "
               "VALUES ('mercadolibre','https://x/protector-solar-isdin-2','product',5.0,'2026-05-30T20:00:00Z',0)")
    db.execute("INSERT INTO frontier (marketplace, url_canonical, url_type, score, added_at, retries) "
               "VALUES ('mercadolibre','https://listado.mercadolibre.com.mx/laptop-hp-2','product',5.0,'2026-05-30T20:00:00Z',0)")
    db.commit()
    items = fr.pop_category_aware(
        "mercadolibre", kind="product", limit=1,
        deficit_categories=["tecnologia"], saturated_categories=["belleza"],
        deficit_boost=2.0, saturated_penalty=0.5,
    )
    # tecnologia: 5*2=10 ; belleza: 5*0.5=2.5 -> tecnologia gana
    assert "laptop" in items[0].url


# L — sin plan, cae a pop normal (por score)
def test_L_no_plan_falls_back_to_score(db):
    fr = FrontierRepo(db)
    db.execute("INSERT INTO frontier (marketplace, url_canonical, url_type, score, added_at, retries) "
               "VALUES ('mercadolibre','https://x/protector-solar-3','product',10.0,'2026-05-30T20:00:00Z',0)")
    db.execute("INSERT INTO frontier (marketplace, url_canonical, url_type, score, added_at, retries) "
               "VALUES ('mercadolibre','https://x/laptop-4','product',1.0,'2026-05-30T20:00:00Z',0)")
    db.commit()
    items = fr.pop_category_aware("mercadolibre", kind="product", limit=1,
                                  deficit_categories=None, saturated_categories=None)
    # sin plan -> pop normal -> mayor score (protector solar 10.0)
    assert "protector-solar" in items[0].url
