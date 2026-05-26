"""Tests del FrontierRepo."""

from __future__ import annotations

from pathlib import Path

import pytest

from ofertas_hunter.db import connect, init_db
from ofertas_hunter.exploration.frontier import FrontierRepo


def test_add_and_pop_product(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    f = FrontierRepo(conn)

    assert f.add("https://www.amazon.com.mx/dp/B0EXAMPLE1") is not None
    assert f.add("https://www.amazon.com.mx/dp/B0EXAMPLE2") is not None
    assert f.count_pending("amazon") == 2

    items = f.pop("amazon", limit=10)
    assert len(items) == 2
    assert all(it.kind == "product" for it in items)
    # Después del pop, frontier vacío.
    assert f.count_pending("amazon") == 0
    conn.close()


def test_add_skips_unknown_urls(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    f = FrontierRepo(conn)

    assert f.add("https://www.amazon.com.mx/ap/signin") is None
    assert f.add("https://www.mercadolibre.com.mx/login") is None
    assert f.add("https://walmart.com.mx/p/x") is None
    assert f.count_pending("amazon") == 0
    assert f.count_pending("mercadolibre") == 0
    conn.close()


def test_add_skips_duplicates(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    f = FrontierRepo(conn)

    url = "https://www.amazon.com.mx/dp/B0EXAMPLE1"
    first = f.add(url)
    assert first is not None
    second = f.add(url)
    # SQLite UNIQUE → segundo INSERT OR IGNORE no añade.
    assert f.count_pending("amazon") == 1
    conn.close()


def test_add_skips_visited(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    f = FrontierRepo(conn)

    url = "https://www.amazon.com.mx/dp/B0EXAMPLE1"
    f.mark_visited("amazon", url)
    assert f.add(url) is None
    assert f.count_pending("amazon") == 0
    assert f.count_visited("amazon") == 1
    conn.close()


def test_pop_filtered_by_kind(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    f = FrontierRepo(conn)

    f.add("https://www.amazon.com.mx/dp/B0EXAMPLE1")  # product
    f.add("https://www.amazon.com.mx/s?k=laptop")  # listing
    f.add("https://www.amazon.com.mx/deals")  # deals

    products = f.pop("amazon", kind="product", limit=10)
    assert len(products) == 1
    assert products[0].kind == "product"

    # Quedan 2 sin sacar.
    assert f.count_pending("amazon") == 2

    listings = f.pop("amazon", kind="listing", limit=10)
    assert len(listings) == 1
    deals = f.pop("amazon", kind="deals", limit=10)
    assert len(deals) == 1
    conn.close()


def test_pop_orders_by_score_desc(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    f = FrontierRepo(conn)

    # listing score 3, deals 5 (default por classifier), product 10.
    f.add("https://www.amazon.com.mx/s?k=foo")
    f.add("https://www.amazon.com.mx/deals")
    f.add("https://www.amazon.com.mx/dp/B0EXAMPLE1")

    items = f.pop("amazon", limit=10)
    assert items[0].kind == "product"
    assert items[1].kind == "deals"
    assert items[2].kind == "listing"
    conn.close()


def test_marketplace_isolation(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    f = FrontierRepo(conn)

    f.add("https://www.amazon.com.mx/dp/B0EXAMPLE1")
    f.add("https://articulo.mercadolibre.com.mx/MLM12345678")

    assert f.count_pending("amazon") == 1
    assert f.count_pending("mercadolibre") == 1

    a = f.pop("amazon", limit=10)
    assert len(a) == 1
    # Pop de amazon no afecta a mercadolibre.
    assert f.count_pending("mercadolibre") == 1
    conn.close()
