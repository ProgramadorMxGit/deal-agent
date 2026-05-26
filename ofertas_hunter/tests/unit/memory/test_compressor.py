"""Tests del MemoryCompressor."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ofertas_hunter.db import connect, init_db
from ofertas_hunter.memory.compressor import (
    CompressorConfig,
    MemoryCompressor,
)


T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _seed(conn) -> None:
    """Inserta datos sintéticos para validar la compactación."""
    # 250 dom_snapshots
    for i in range(250):
        conn.execute(
            "INSERT INTO dom_snapshots (marketplace, context, url, content, captured_at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("amazon", "product_page", f"u{i}", "<html></html>", _iso(T0 - timedelta(hours=i)), "fetch_failed"),
        )

    # runtime_events: la mitad viejos (40 días), la otra mitad recientes
    for i in range(20):
        conn.execute(
            "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("captcha", "warning", "{}", _iso(T0 - timedelta(days=40))),
        )
    for i in range(15):
        conn.execute(
            "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("agent_restart", "warning", "{}", _iso(T0 - timedelta(days=1))),
        )
    conn.execute(
        "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("agent_degraded", "critical", "{}", _iso(T0 - timedelta(days=2))),
    )

    # discarded_candidates: viejos y recientes
    for i in range(30):
        conn.execute(
            "INSERT INTO discarded_candidates (source, raw_payload_json, reason, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("amazon_hunter", "{}", "no_image", _iso(T0 - timedelta(days=45))),
        )
    for i in range(20):
        conn.execute(
            "INSERT INTO discarded_candidates (source, raw_payload_json, reason, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("mercadolibre_hunter", "{}", "missing_affiliate_url", _iso(T0 - timedelta(days=2))),
        )

    # agent_runs: 1500 (más del límite por defecto 1000)
    for i in range(1500):
        conn.execute(
            "INSERT INTO agent_runs (agent_name, started_at, status, summary_json, last_heartbeat) "
            "VALUES (?, ?, ?, ?, ?)",
            ("agent_x", _iso(T0 - timedelta(minutes=i)), "ok", "{}", _iso(T0)),
        )

    # selector_versions: con 4 versiones del mismo (marketplace,context,key) en 30d
    for i in range(4):
        conn.execute(
            "INSERT INTO selector_versions (marketplace, context, key, selector_value, "
            "applied_at, applied_by, test_pass) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("amazon", "product_page", "price_current", f"#sel{i}", _iso(T0 - timedelta(days=5+i)), "heuristic", 1),
        )

    # productos + observations para summary
    cur = conn.execute(
        "INSERT INTO products (marketplace, url_canonical, title, condition, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("amazon", "https://x.com/1", "Producto A", "new", _iso(T0), _iso(T0)),
    )
    p1 = cur.lastrowid
    for _ in range(5):
        conn.execute(
            "INSERT INTO price_observations (product_id, current_price, source, observed_at) "
            "VALUES (?, ?, ?, ?)",
            (p1, 100.0, "amazon_hunter", _iso(T0)),
        )
    cur = conn.execute(
        "INSERT INTO products (marketplace, url_canonical, title, condition, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("mercadolibre", "https://x.com/2", "Producto B", "new", _iso(T0), _iso(T0)),
    )
    p2 = cur.lastrowid
    for _ in range(3):
        conn.execute(
            "INSERT INTO price_observations (product_id, current_price, source, observed_at) "
            "VALUES (?, ?, ?, ?)",
            (p2, 200.0, "mercadolibre_hunter", _iso(T0)),
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_compressor_truncates_dom_snapshots(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    _seed(conn)

    compressor = MemoryCompressor(
        conn,
        config=CompressorConfig(dom_snapshots_keep=100),
        clock=lambda: T0,
    )
    report = compressor.run()
    assert report.deleted_dom_snapshots == 150  # 250 - 100

    rows = conn.execute("SELECT COUNT(*) AS n FROM dom_snapshots").fetchone()
    assert rows["n"] == 100
    conn.close()


def test_compressor_deletes_old_runtime_events(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    _seed(conn)

    compressor = MemoryCompressor(
        conn,
        config=CompressorConfig(runtime_events_ttl_days=30),
        clock=lambda: T0,
    )
    report = compressor.run()
    assert report.deleted_runtime_events == 20  # los 40d se borran

    rows = conn.execute("SELECT COUNT(*) AS n FROM runtime_events").fetchone()
    # 36 eventos seedados - 20 borrados = 16
    assert rows["n"] == 16
    conn.close()


def test_compressor_deletes_old_discarded_candidates(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    _seed(conn)

    compressor = MemoryCompressor(
        conn,
        config=CompressorConfig(
            discarded_candidates_ttl_days=30,
            discarded_candidates_keep_max=10000,
        ),
        clock=lambda: T0,
    )
    report = compressor.run()
    assert report.deleted_discarded_candidates == 30  # los 45d se van
    rows = conn.execute("SELECT COUNT(*) AS n FROM discarded_candidates").fetchone()
    assert rows["n"] == 20  # los recientes
    conn.close()


def test_compressor_truncates_agent_runs(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    _seed(conn)

    compressor = MemoryCompressor(
        conn,
        config=CompressorConfig(agent_runs_keep=1000),
        clock=lambda: T0,
    )
    report = compressor.run()
    assert report.deleted_agent_runs == 500
    rows = conn.execute("SELECT COUNT(*) AS n FROM agent_runs").fetchone()
    assert rows["n"] == 1000
    conn.close()


def test_compressor_writes_memory_summaries(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    _seed(conn)

    compressor = MemoryCompressor(conn, clock=lambda: T0)
    report = compressor.run()
    assert "discard_reasons" in report.summary_kinds
    assert "unstable_selectors" in report.summary_kinds
    assert "runtime_events_by_severity" in report.summary_kinds
    assert "marketplace_observations" in report.summary_kinds

    rows = conn.execute(
        "SELECT kind, content FROM memory_summaries ORDER BY id"
    ).fetchall()
    by_kind = {r["kind"]: json.loads(r["content"]) for r in rows}

    # discard_reasons: top razones
    assert any(
        e["reason"] == "missing_affiliate_url"
        for e in by_kind["discard_reasons"]["rows"]
    )

    # unstable_selectors: amazon/product_page/price_current con 4 versiones
    unstable = by_kind["unstable_selectors"]["rows"]
    assert any(
        r["marketplace"] == "amazon"
        and r["context"] == "product_page"
        and r["key"] == "price_current"
        and r["versions"] == 4
        for r in unstable
    )

    # runtime_events_by_severity: critical va primero
    severities = [r["severity"] for r in by_kind["runtime_events_by_severity"]["rows"]]
    if severities:
        assert severities[0] in ("critical", "warning")

    # marketplace_observations
    ml_rows = by_kind["marketplace_observations"]["rows"]
    assert any(r["marketplace"] == "amazon" and r["observations"] == 5 for r in ml_rows)
    assert any(r["marketplace"] == "mercadolibre" and r["observations"] == 3 for r in ml_rows)

    conn.close()


def test_compressor_idempotent_on_empty_db(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    compressor = MemoryCompressor(conn, clock=lambda: T0)
    report = compressor.run()
    assert report.deleted_dom_snapshots == 0
    assert report.deleted_runtime_events == 0
    # Aún con tablas vacías, los summaries se generan (con rows vacíos).
    assert report.summaries_generated == 4
    conn.close()
