"""Smoke test: la migración SQL inicializa el esquema sin errores."""

from __future__ import annotations

from pathlib import Path

import pytest

from ofertas_hunter.db import connection, init_db


def test_init_db_creates_all_tables(tmp_path: Path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    assert db_path.exists()

    expected = {
        "products",
        "price_observations",
        "offers",
        "outbox",
        "published_messages",
        "discarded_candidates",
        "visited_urls",
        "frontier",
        "dom_snapshots",
        "selector_versions",
        "agent_runs",
        "self_patches",
        "memory_summaries",
        "runtime_events",
        "telegram_messages",
        "price_error_examples",
        "category_price_ranges",
        "product_aliases",
        "resolved_urls",
    }
    with connection(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        names = {r["name"] for r in rows}
    missing = expected - names
    assert not missing, f"faltan tablas: {missing}"


def test_init_db_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "idem.db"
    init_db(db_path)
    init_db(db_path)  # segunda vez no rompe
    with connection(db_path) as conn:
        ((cnt,),) = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchall()
    assert cnt > 0


def test_wal_mode_enabled(tmp_path: Path):
    db_path = tmp_path / "wal.db"
    init_db(db_path)
    with connection(db_path) as conn:
        ((mode,),) = conn.execute("PRAGMA journal_mode").fetchall()
    assert mode.lower() == "wal"
