"""Tests de la purga de runtime_events para nightly_maintenance.

Reglas:
- Solo borra `mcp_tool_called` con created_at < ahora - keep_mcp_hours.
- Conserva eventos importantes (errores, captcha, sesión, etc.) sin importar edad.
- Conserva `mcp_tool_called` reciente (< corte).
- En dry-run no borra nada.
- Corre en batches y reporta conteos.
- No toca otras tablas.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ofertas_hunter.db import connect, init_db
from ofertas_hunter.maintenance.nightly import purge_runtime_events


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@pytest.fixture
def conn(tmp_path: Path):
    init_db(tmp_path / "x.db")
    c = connect(tmp_path / "x.db")
    yield c
    c.close()


def _ev(conn, kind, created_at, severity="info"):
    conn.execute(
        "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (kind, severity, "{}", created_at),
    )
    conn.commit()


def _count(conn, kind=None):
    if kind:
        return conn.execute("SELECT COUNT(*) FROM runtime_events WHERE kind=?", (kind,)).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM runtime_events").fetchone()[0]


def _seed(conn, now):
    old = _iso(now - timedelta(hours=72))   # > 48h
    recent = _iso(now - timedelta(hours=2))  # < 48h
    # mcp_tool_called viejo (borrable) y reciente (conservar)
    for _ in range(2500):
        _ev(conn, "mcp_tool_called", old)
    for _ in range(10):
        _ev(conn, "mcp_tool_called", recent)
    # eventos importantes viejos -> SIEMPRE conservar
    _ev(conn, "amazon_legacy_captcha_suspect", old, "warning")
    _ev(conn, "cookie_expiry", old, "warning")
    _ev(conn, "false_price_error_detected", old, "error")
    _ev(conn, "ml_session_state_changed", old, "info")


def test_purge_deletes_only_old_mcp_tool_called(conn):
    now = datetime(2026, 5, 30, 4, 0, tzinfo=timezone.utc)
    _seed(conn, now)
    before = _count(conn)

    report = purge_runtime_events(
        conn, now=now, keep_mcp_hours=48, batch_size=1000, dry_run=False
    )

    assert report.rows_before == before
    assert report.rows_deleted == 2500
    assert report.rows_after == before - 2500
    # recientes mcp intactos
    assert _count(conn, "mcp_tool_called") == 10
    # eventos importantes intactos aunque viejos
    assert _count(conn, "amazon_legacy_captcha_suspect") == 1
    assert _count(conn, "cookie_expiry") == 1
    assert _count(conn, "false_price_error_detected") == 1
    assert _count(conn, "ml_session_state_changed") == 1


def test_purge_dry_run_deletes_nothing(conn):
    now = datetime(2026, 5, 30, 4, 0, tzinfo=timezone.utc)
    _seed(conn, now)
    before = _count(conn)

    report = purge_runtime_events(
        conn, now=now, keep_mcp_hours=48, batch_size=1000, dry_run=True
    )

    assert report.dry_run is True
    assert report.rows_candidate == 2500
    assert report.rows_deleted == 0
    assert _count(conn) == before


def test_purge_runs_in_batches(conn):
    now = datetime(2026, 5, 30, 4, 0, tzinfo=timezone.utc)
    _seed(conn, now)
    report = purge_runtime_events(
        conn, now=now, keep_mcp_hours=48, batch_size=500, dry_run=False
    )
    assert report.rows_deleted == 2500
    assert report.batches >= 5


def test_purge_does_not_touch_other_tables(conn):
    now = datetime(2026, 5, 30, 4, 0, tzinfo=timezone.utc)
    _seed(conn, now)
    # Sembrar tablas críticas
    conn.execute(
        "INSERT INTO products (marketplace, marketplace_id, url_canonical, title, "
        "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
        ("amazon", "X1", "https://x/1", "t", _iso(now), _iso(now)),
    )
    conn.execute(
        "INSERT INTO frontier (marketplace, url_canonical, url_type, score, added_at, retries) "
        "VALUES (?,?,?,?,?,?)",
        ("mercadolibre", "https://f/1", "listing", 1.0, _iso(now), 0),
    )
    conn.commit()
    prod_before = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    front_before = conn.execute("SELECT COUNT(*) FROM frontier").fetchone()[0]

    purge_runtime_events(conn, now=now, keep_mcp_hours=48, batch_size=1000, dry_run=False)

    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == prod_before
    assert conn.execute("SELECT COUNT(*) FROM frontier").fetchone()[0] == front_before
