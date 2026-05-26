"""Tests para las 5 read tools (`get_status`, `get_schedule_mode`, `get_outbox`,
`get_recent_events`, `get_frontier_stats`)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ofertas_hunter.config import Settings
from ofertas_hunter.db import connect, init_db
from ofertas_hunter.mcp.context import ServerContext
from ofertas_hunter.mcp.server import MCPServer
from ofertas_hunter.mcp.tools.read_tools import build_read_tools


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    init_db(tmp_path / "x.db")
    c = connect(tmp_path / "x.db")
    yield c
    c.close()


@pytest.fixture
def ctx(db) -> ServerContext:
    return ServerContext.build(db=db, settings=Settings())


@pytest.fixture
def server(ctx) -> MCPServer:
    registry = {spec.name: spec for spec in build_read_tools(ctx)}
    return MCPServer(ctx, registry=registry)


_seed_counter = [0]


def _seed_outbox(db: sqlite3.Connection, *, type_: str, payload: dict | None = None) -> int:
    if payload is None:
        payload = {"image_url": "https://x", "current_price": 1.0}
    _seed_counter[0] += 1
    n = _seed_counter[0]
    cur = db.execute(
        "INSERT INTO products (marketplace, marketplace_id, url_canonical, title, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("amazon", f"B{type_}_{n}", f"https://x/{type_}/{n}", "T", "2026-01-01", "2026-01-01"),
    )
    pid = cur.lastrowid
    cur = db.execute(
        "INSERT INTO offers (product_id, classification, score, reasons_json, state, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pid, "no_price_error", 0, "[]", "eligible", "2026-01-01", "2026-01-01"),
    )
    oid = cur.lastrowid
    cur = db.execute(
        "INSERT INTO outbox (offer_id, type, enqueued_at, attempts, state, "
        "message_payload_json) VALUES (?, ?, ?, ?, ?, ?)",
        (oid, type_, "2026-01-01T00:00:00Z", 0, "pending", json.dumps(payload)),
    )
    db.commit()
    return cur.lastrowid


def _seed_event(db: sqlite3.Connection, severity: str = "info", kind: str = "test") -> None:
    db.execute(
        "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
        "VALUES (?, ?, '{}', '2026-01-01T00:00:00Z')",
        (kind, severity),
    )
    db.commit()


def _seed_frontier(db: sqlite3.Connection, marketplace: str, url_type: str, n: int) -> None:
    for i in range(n):
        db.execute(
            "INSERT INTO frontier (marketplace, url_canonical, url_type, score, added_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (marketplace, f"https://x/{url_type}/{i}", url_type, 0.0, "2026-01-01"),
        )
    db.commit()


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_status_returns_schedule_mode_and_flags(server) -> None:
    out = await server.dispatch("get_status", {})
    assert "schedule_mode" in out
    assert "publishing_enabled" in out
    assert "publishing_dry_run" in out
    assert "agents_registered" in out


@pytest.mark.asyncio
async def test_get_status_includes_outbox_summary(server, db) -> None:
    _seed_outbox(db, type_="normal")
    _seed_outbox(db, type_="price_error")
    out = await server.dispatch("get_status", {})
    assert "normal" in out["outbox_summary"]
    assert "price_error" in out["outbox_summary"]


@pytest.mark.asyncio
async def test_get_status_rejects_extra_args(server) -> None:
    out = await server.dispatch("get_status", {"foo": 1})
    assert out["error"] == "validation_failed"


# ---------------------------------------------------------------------------
# get_schedule_mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_schedule_mode_returns_next_change_in_seconds_int(server) -> None:
    out = await server.dispatch("get_schedule_mode", {})
    assert isinstance(out["next_change_in_seconds"], int)
    assert out["mode"] in ("active", "hibernating", "warmup")


# ---------------------------------------------------------------------------
# get_outbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_outbox_respects_limit(server, db) -> None:
    for _ in range(5):
        _seed_outbox(db, type_="normal")
    out = await server.dispatch("get_outbox", {"limit": 3})
    assert out["meta"]["count"] == 3


@pytest.mark.asyncio
async def test_get_outbox_filters_by_type_when_not_any(server, db) -> None:
    _seed_outbox(db, type_="normal")
    _seed_outbox(db, type_="price_error")
    out = await server.dispatch("get_outbox", {"type_filter": "price_error"})
    assert all(item["type"] == "price_error" for item in out["data"])


@pytest.mark.asyncio
async def test_get_outbox_orders_desc_by_enqueued_at(server, db) -> None:
    # 3 items con timestamps explícitos
    for ts in ["2026-01-01", "2026-01-02", "2026-01-03"]:
        cur = db.execute(
            "INSERT INTO products (marketplace, marketplace_id, url_canonical, title, "
            "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("amazon", ts, f"https://x/{ts}", "T", "2026-01-01", "2026-01-01"),
        )
        pid = cur.lastrowid
        cur = db.execute(
            "INSERT INTO offers (product_id, classification, score, reasons_json, state, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, "no_price_error", 0, "[]", "eligible", ts, ts),
        )
        oid = cur.lastrowid
        db.execute(
            "INSERT INTO outbox (offer_id, type, enqueued_at, attempts, state, "
            "message_payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (oid, "normal", ts, 0, "pending", "{}"),
        )
    db.commit()
    out = await server.dispatch("get_outbox", {})
    timestamps = [item["enqueued_at"] for item in out["data"]]
    assert timestamps == sorted(timestamps, reverse=True)


# ---------------------------------------------------------------------------
# get_recent_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_recent_events_respects_severity_filter(server, db) -> None:
    _seed_event(db, severity="info")
    _seed_event(db, severity="warning")
    _seed_event(db, severity="error")
    out = await server.dispatch("get_recent_events", {"severity": "warning"})
    assert all(e["severity"] == "warning" for e in out["data"])


@pytest.mark.asyncio
async def test_get_recent_events_respects_limit(server, db) -> None:
    for _ in range(10):
        _seed_event(db)
    out = await server.dispatch("get_recent_events", {"limit": 4})
    assert out["meta"]["count"] == 4


# ---------------------------------------------------------------------------
# get_frontier_stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_frontier_stats_returns_zero_when_empty(server) -> None:
    out = await server.dispatch("get_frontier_stats", {"marketplace": "amazon"})
    assert out["total"] == 0


@pytest.mark.asyncio
async def test_get_frontier_stats_groups_by_url_type(server, db) -> None:
    _seed_frontier(db, "amazon", "product", 3)
    _seed_frontier(db, "amazon", "listing", 2)
    out = await server.dispatch("get_frontier_stats", {"marketplace": "amazon"})
    assert out["by_url_type"]["product"] == 3
    assert out["by_url_type"]["listing"] == 2
    assert out["total"] == 5


@pytest.mark.asyncio
async def test_get_frontier_stats_isolates_by_marketplace(server, db) -> None:
    _seed_frontier(db, "amazon", "product", 3)
    _seed_frontier(db, "mercadolibre", "product", 7)
    out_amz = await server.dispatch("get_frontier_stats", {"marketplace": "amazon"})
    out_ml = await server.dispatch("get_frontier_stats", {"marketplace": "mercadolibre"})
    assert out_amz["total"] == 3
    assert out_ml["total"] == 7


@pytest.mark.asyncio
async def test_get_frontier_stats_rejects_unknown_marketplace(server) -> None:
    out = await server.dispatch("get_frontier_stats", {"marketplace": "walmart"})
    assert out["error"] == "validation_failed"


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


def test_build_read_tools_returns_five_specs(ctx) -> None:
    specs = build_read_tools(ctx)
    names = {s.name for s in specs}
    assert names == {
        "get_status",
        "get_schedule_mode",
        "get_outbox",
        "get_recent_events",
        "get_frontier_stats",
    }


def test_each_read_tool_descriptor_has_additional_properties_false(ctx) -> None:
    specs = build_read_tools(ctx)
    for spec in specs:
        d = spec.descriptor()
        assert d.inputSchema.get("additionalProperties") is False, (
            f"{spec.name} permite props extra"
        )
