"""Smoke test in-process del MCP server.

Construye un `MCPServer` con context real (DB temporal) y dispara las 16
tools (5 read + 7 action + 4 quality) verificando que cada una devuelva una
respuesta del shape esperado. Las action tools que requieren browser real
son mockeadas con fakes.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import time, timedelta
from pathlib import Path
from typing import Any

import pytest

from ofertas_hunter.config import Settings
from ofertas_hunter.db import connect, init_db
from ofertas_hunter.mcp.context import ServerContext
from ofertas_hunter.mcp.server import MCPServer
from ofertas_hunter.runtime.scheduler import ModeDecision, ScheduleMode


CANONICAL_TOOLS = (
    # read (5)
    "get_status",
    "get_schedule_mode",
    "get_outbox",
    "get_recent_events",
    "get_frontier_stats",
    # action (7)
    "discover_seeds",
    "hunt_amazon",
    "hunt_mercadolibre",
    "dispatch_outbox",
    "revalidate_offer",
    "pause_marketplace",
    "unpause_marketplace",
    # quality (4)
    "request_offer_review",
    "submit_offer_review",
    "improve_message_copy",
    "submit_message_copy",
)


# ---------------------------------------------------------------------------
# Fakes (sin Playwright)
# ---------------------------------------------------------------------------


@dataclass
class _FakeOutcome:
    url: str = "https://x"
    final_url: str = "https://x"
    classification: str = "no_price_error"
    suggested_outbox_type: str | None = None
    enqueued_outbox_id: int | None = None
    discarded_reason: str | None = None


@dataclass
class _FakeHunter:
    paused: bool = False

    async def hunt_from_frontier(self, *, max_urls: int = 5):
        return [_FakeOutcome()]


@dataclass
class _FakeDiscoveryOutcome:
    url: str = "https://x"
    kind: str = "listing"
    discovered_count: int = 0
    persisted_count: int = 0
    discarded_reason: str | None = None


@dataclass
class _FakeDiscovery:
    max_per_cycle: int = 4

    async def discover_once(self):
        return [_FakeDiscoveryOutcome()]


@dataclass
class _FakeRevalidation:
    ok: bool = True
    classification: str = "normal_offer"
    fatal_reason: str | None = None
    reasons: list = None
    extracted: object = None
    snapshot_saved: int | None = None
    suggested_outbox_type: str = "normal"
    confidence_label: str = "low"


@dataclass
class _FakeRevalidator:
    async def revalidate_detailed(self, item):
        return _FakeRevalidation(reasons=[])


@dataclass
class _FakePublish:
    success: bool = True
    dry_run: bool = True
    formatted: Any = None
    evolution_response: Any = None
    skipped: bool = False
    skip_reason: str | None = None
    error: str | None = None


@dataclass
class _FakeDispatcher:
    calls: int = 0

    async def tick(self):
        if self.calls > 0:
            return None
        self.calls += 1
        return _FakePublish()


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
    c = ServerContext.build(db=db, settings=Settings())

    # Fakes en lugar de browser/playwright
    async def _amz():
        return _FakeHunter()

    async def _ml():
        return _FakeHunter()

    async def _ad():
        return _FakeDiscovery()

    async def _md():
        return _FakeDiscovery()

    async def _rev():
        return _FakeRevalidator()

    c.get_amazon_hunter = _amz
    c.get_ml_hunter = _ml
    c.get_amazon_discovery = _ad
    c.get_ml_discovery = _md
    c.get_revalidator = _rev
    c.get_dispatcher = lambda: _FakeDispatcher()
    return c


@pytest.fixture
def server(ctx) -> MCPServer:
    return MCPServer(ctx)  # registry construido por defecto con todas las tools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_outbox(db: sqlite3.Connection, payload: dict | None = None) -> int:
    if payload is None:
        payload = {
            "title": "JBL Tune 510BT",
            "image_url": "https://x/img.jpg",
            "current_price": 388.0,
            "previous_price": 899.0,
            "discount_percent": 57.0,
            "url": "https://amzn.to/4e3yTjG",
            "marketplace": "amazon",
        }
    cur = db.execute(
        "INSERT INTO products (marketplace, marketplace_id, url_canonical, title, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("amazon", "B0", "https://x/p", "T", "2026-01-01", "2026-01-01"),
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
        (oid, "normal", "2026-01-01T00:00:00Z", 0, "pending", json.dumps(payload)),
    )
    db.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


def test_handshake_announces_16_canonical_tools(server) -> None:
    names = set(server.registry.keys())
    expected = set(CANONICAL_TOOLS)
    assert names == expected, f"diff: {expected.symmetric_difference(names)}"


def test_each_descriptor_has_additional_properties_false(server) -> None:
    for spec in server.registry.values():
        d = spec.descriptor()
        assert d.inputSchema.get("additionalProperties") is False, spec.name


@pytest.mark.asyncio
async def test_dispatch_each_read_tool_returns_data_shape(server, db) -> None:
    out_status = await server.dispatch("get_status", {})
    assert "schedule_mode" in out_status

    out_sched = await server.dispatch("get_schedule_mode", {})
    assert out_sched["mode"] in ("active", "hibernating", "warmup")

    out_outbox = await server.dispatch("get_outbox", {})
    assert "data" in out_outbox and "meta" in out_outbox

    out_events = await server.dispatch("get_recent_events", {})
    assert "data" in out_events

    out_frontier = await server.dispatch(
        "get_frontier_stats", {"marketplace": "amazon"}
    )
    assert "total" in out_frontier


@pytest.mark.asyncio
async def test_dispatch_each_action_tool_returns_shape(server, db) -> None:
    # discover_seeds
    out = await server.dispatch(
        "discover_seeds", {"marketplace": "amazon", "limit": 2}
    )
    assert out.get("success") is True or out.get("skipped") is True

    # hunt_amazon / hunt_mercadolibre
    out = await server.dispatch("hunt_amazon", {"limit": 2})
    assert out.get("success") is True or out.get("skipped") is True

    out = await server.dispatch("hunt_mercadolibre", {"limit": 2})
    assert out.get("success") is True or out.get("skipped") is True

    # dispatch_outbox
    out = await server.dispatch("dispatch_outbox", {"limit": 1})
    assert "ticks" in out or out.get("skipped") is True

    # pause / unpause
    out = await server.dispatch(
        "pause_marketplace", {"marketplace": "amazon", "reason": "smoke"}
    )
    assert out["success"] is True
    out = await server.dispatch("unpause_marketplace", {"marketplace": "amazon"})
    assert out["success"] is True


@pytest.mark.asyncio
async def test_dispatch_revalidate_returns_classification(server, db) -> None:
    oid = _seed_outbox(db)
    out = await server.dispatch("revalidate_offer", {"outbox_id": oid})
    assert out["success"] is True
    assert out["classification"] == "normal_offer"


@pytest.mark.asyncio
async def test_dispatch_request_and_submit_offer_review_round_trip(server, db) -> None:
    oid = _seed_outbox(db)
    req = await server.dispatch("request_offer_review", {"outbox_id": oid})
    token = req["review_token"]
    out = await server.dispatch(
        "submit_offer_review",
        {"outbox_id": oid, "review_token": token, "decision": "approve"},
    )
    assert out["success"] is True


@pytest.mark.asyncio
async def test_dispatch_improve_and_submit_message_copy_round_trip(server, db) -> None:
    oid = _seed_outbox(db)
    req = await server.dispatch("improve_message_copy", {"outbox_id": oid})
    token = req["review_token"]
    out = await server.dispatch(
        "submit_message_copy",
        {"outbox_id": oid, "review_token": token, "new_text": "Nuevo copy"},
    )
    assert out["success"] is True
    # Caption se aplicó
    row = db.execute(
        "SELECT message_payload_json FROM outbox WHERE id = ?", (oid,)
    ).fetchone()
    assert json.loads(row["message_payload_json"])["caption_override"] == "Nuevo copy"


@pytest.mark.asyncio
async def test_audit_emits_two_events_per_call_in_runtime_events(server, db) -> None:
    db.execute("DELETE FROM runtime_events")
    db.commit()
    await server.dispatch("get_status", {})
    rows = db.execute(
        "SELECT severity, payload_json FROM runtime_events "
        "WHERE kind='mcp_tool_called'"
    ).fetchall()
    phases = [json.loads(r["payload_json"])["phase"] for r in rows]
    assert "before" in phases and "after" in phases
