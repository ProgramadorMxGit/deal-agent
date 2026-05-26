"""Tests para las 7 action tools del MCP server.

Usa fakes de hunters/dispatcher inyectados directamente en `ServerContext`
para evitar Playwright real en tests.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import time, timedelta
from pathlib import Path
from typing import Any

import pytest

from ofertas_hunter.config import Settings
from ofertas_hunter.db import connect, init_db
from ofertas_hunter.mcp.context import ServerContext
from ofertas_hunter.mcp.server import MCPServer
from ofertas_hunter.mcp.tools.action_tools import build_action_tools
from ofertas_hunter.runtime.scheduler import ModeDecision, ScheduleMode


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeOutcome:
    url: str = "https://x"
    final_url: str = "https://x"
    classification: str = "no_price_error"
    suggested_outbox_type: str | None = "normal"
    enqueued_outbox_id: int | None = 1
    discarded_reason: str | None = None


@dataclass
class _FakeDiscoveryOutcome:
    url: str = "https://x"
    kind: str = "listing"
    discovered_count: int = 0
    persisted_count: int = 0
    discarded_reason: str | None = None


@dataclass
class _FakeHunter:
    paused: bool = False
    calls: int = 0

    async def hunt_from_frontier(self, *, max_urls: int = 5):
        self.calls += 1
        return [_FakeOutcome() for _ in range(min(max_urls, 2))]


@dataclass
class _FakeDiscovery:
    max_per_cycle: int = 4
    calls: int = 0

    async def discover_once(self):
        self.calls += 1
        return [_FakeDiscoveryOutcome(discovered_count=3, persisted_count=2)]


@dataclass
class _FakePublishOutcome:
    success: bool = True
    dry_run: bool = True
    formatted: Any = None
    evolution_response: Any = None
    skipped: bool = False
    skip_reason: str | None = None
    error: str | None = None


@dataclass
class _FakeDispatcher:
    ticks: int = 0
    max_ticks: int = 1

    async def tick(self):
        if self.ticks >= self.max_ticks:
            return None
        self.ticks += 1
        return _FakePublishOutcome()


@dataclass
class _FakeScheduler:
    mode: ScheduleMode = ScheduleMode.ACTIVE

    def decide(self):
        return ModeDecision(
            mode=self.mode,
            local_time=time(12, 0),
            next_change_in=timedelta(hours=11),
            next_mode=ScheduleMode.HIBERNATING,
        )


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
    c.scheduler = _FakeScheduler()
    return c


@pytest.fixture
def server(ctx) -> MCPServer:
    registry = {spec.name: spec for spec in build_action_tools(ctx)}
    return MCPServer(ctx, registry=registry)


def _patch_agents(ctx: ServerContext, *, hunter=None, ml_hunter=None,
                  discovery=None, ml_discovery=None, dispatcher=None):
    """Inyecta fakes en los slots lazy del context."""
    if hunter is not None:
        ctx._amazon_hunter = hunter

        async def _amz():
            return ctx._amazon_hunter

        ctx.get_amazon_hunter = _amz
    if ml_hunter is not None:
        ctx._ml_hunter = ml_hunter

        async def _ml():
            return ctx._ml_hunter

        ctx.get_ml_hunter = _ml
    if discovery is not None:
        ctx._amazon_discovery = discovery

        async def _ad():
            return ctx._amazon_discovery

        ctx.get_amazon_discovery = _ad
    if ml_discovery is not None:
        ctx._ml_discovery = ml_discovery

        async def _md():
            return ctx._ml_discovery

        ctx.get_ml_discovery = _md
    if dispatcher is not None:
        ctx._dispatcher = dispatcher
        ctx.get_dispatcher = lambda: ctx._dispatcher


# ---------------------------------------------------------------------------
# pause / unpause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_marketplace_sets_pause_state_active(server, ctx) -> None:
    out = await server.dispatch(
        "pause_marketplace",
        {"marketplace": "amazon", "reason": "captcha_burst"},
    )
    assert out["success"] is True
    assert ctx.pause_state["amazon"].active is True
    assert ctx.pause_state["amazon"].reason == "captcha_burst"


@pytest.mark.asyncio
async def test_pause_marketplace_with_ttl_clears_after_ttl(server, ctx) -> None:
    await server.dispatch(
        "pause_marketplace",
        {"marketplace": "amazon", "reason": "x", "ttl_seconds": 1},
    )
    assert ctx.pause_state["amazon"].active is True
    await asyncio.sleep(1.05)
    assert ctx.pause_state["amazon"].active is False


@pytest.mark.asyncio
async def test_unpause_marketplace_clears(server, ctx) -> None:
    await server.dispatch(
        "pause_marketplace", {"marketplace": "amazon", "reason": "x"}
    )
    out = await server.dispatch("unpause_marketplace", {"marketplace": "amazon"})
    assert out["success"] is True and out["was_paused"] is True
    assert "amazon" not in ctx.pause_state


@pytest.mark.asyncio
async def test_pause_emits_runtime_event_warning(server, ctx) -> None:
    await server.dispatch(
        "pause_marketplace", {"marketplace": "amazon", "reason": "x"}
    )
    rows = ctx.db.execute(
        "SELECT severity FROM runtime_events WHERE kind = 'mcp_marketplace_paused'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# discover_seeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_seeds_calls_existing_agent_singleton(server, ctx) -> None:
    discovery = _FakeDiscovery()
    _patch_agents(ctx, discovery=discovery)
    out = await server.dispatch(
        "discover_seeds", {"marketplace": "amazon", "limit": 4}
    )
    assert out["success"] is True
    assert out["processed"] == 1
    assert out["discovered"] == 3
    assert discovery.calls == 1


@pytest.mark.asyncio
async def test_discover_seeds_skipped_during_hibernating(server, ctx) -> None:
    ctx.scheduler = _FakeScheduler(mode=ScheduleMode.HIBERNATING)
    discovery = _FakeDiscovery()
    _patch_agents(ctx, discovery=discovery)
    out = await server.dispatch(
        "discover_seeds", {"marketplace": "amazon", "limit": 4}
    )
    assert out["skipped"] is True and out["reason"] == "hibernating"
    assert discovery.calls == 0


@pytest.mark.asyncio
async def test_discover_seeds_skipped_during_warmup(server, ctx) -> None:
    ctx.scheduler = _FakeScheduler(mode=ScheduleMode.WARMUP)
    discovery = _FakeDiscovery()
    _patch_agents(ctx, discovery=discovery)
    out = await server.dispatch(
        "discover_seeds", {"marketplace": "amazon", "limit": 4}
    )
    assert out["skipped"] is True and out["reason"] == "warmup"


@pytest.mark.asyncio
async def test_discover_seeds_skipped_when_marketplace_paused(server, ctx) -> None:
    discovery = _FakeDiscovery()
    _patch_agents(ctx, discovery=discovery)
    await server.dispatch(
        "pause_marketplace", {"marketplace": "amazon", "reason": "x"}
    )
    out = await server.dispatch(
        "discover_seeds", {"marketplace": "amazon", "limit": 4}
    )
    assert out["skipped"] is True and out["reason"] == "paused"


# ---------------------------------------------------------------------------
# hunt_amazon / hunt_mercadolibre
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hunt_amazon_calls_existing_agent_singleton(server, ctx) -> None:
    hunter = _FakeHunter()
    _patch_agents(ctx, hunter=hunter)
    out = await server.dispatch("hunt_amazon", {"limit": 5})
    assert out["success"] is True
    assert hunter.calls == 1


@pytest.mark.asyncio
async def test_hunt_amazon_skipped_during_hibernating(server, ctx) -> None:
    ctx.scheduler = _FakeScheduler(mode=ScheduleMode.HIBERNATING)
    hunter = _FakeHunter()
    _patch_agents(ctx, hunter=hunter)
    out = await server.dispatch("hunt_amazon", {})
    assert out["skipped"] is True and out["reason"] == "hibernating"
    assert hunter.calls == 0


@pytest.mark.asyncio
async def test_hunt_mercadolibre_skipped_when_paused_for_login(server, ctx) -> None:
    hunter = _FakeHunter(paused=True)
    _patch_agents(ctx, ml_hunter=hunter)
    out = await server.dispatch("hunt_mercadolibre", {})
    assert out["skipped"] is True and out["reason"] == "ml_paused_for_login"
    assert hunter.calls == 0


@pytest.mark.asyncio
async def test_hunt_mercadolibre_returns_outcomes_summary(server, ctx) -> None:
    hunter = _FakeHunter()
    _patch_agents(ctx, ml_hunter=hunter)
    out = await server.dispatch("hunt_mercadolibre", {"limit": 3})
    assert out["success"] is True
    assert "outcomes" in out


# ---------------------------------------------------------------------------
# dispatch_outbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_outbox_calls_dispatcher_tick_n_times(server, ctx) -> None:
    dispatcher = _FakeDispatcher(max_ticks=3)
    _patch_agents(ctx, dispatcher=dispatcher)
    out = await server.dispatch("dispatch_outbox", {"limit": 5})
    # tick devolverá None tras 3 invocaciones
    assert dispatcher.ticks == 3
    assert out["ticks"] == 3


@pytest.mark.asyncio
async def test_dispatch_outbox_skipped_during_hibernating(server, ctx) -> None:
    ctx.scheduler = _FakeScheduler(mode=ScheduleMode.HIBERNATING)
    dispatcher = _FakeDispatcher()
    _patch_agents(ctx, dispatcher=dispatcher)
    out = await server.dispatch("dispatch_outbox", {})
    assert out["skipped"] is True
    assert dispatcher.ticks == 0


@pytest.mark.asyncio
async def test_dispatch_outbox_skipped_during_warmup(server, ctx) -> None:
    ctx.scheduler = _FakeScheduler(mode=ScheduleMode.WARMUP)
    out = await server.dispatch("dispatch_outbox", {})
    assert out["skipped"] is True


# ---------------------------------------------------------------------------
# Hard rules cannot be overridden
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hunt_amazon_rejects_extra_args(server) -> None:
    out = await server.dispatch("hunt_amazon", {"force": True})
    assert out["error"] == "validation_failed"


@pytest.mark.asyncio
async def test_dispatch_outbox_rejects_force_arg(server) -> None:
    out = await server.dispatch("dispatch_outbox", {"override_cooldown": True})
    assert out["error"] == "validation_failed"


# ---------------------------------------------------------------------------
# Smoke: build returns 7 specs
# ---------------------------------------------------------------------------


def test_build_action_tools_returns_seven_specs(ctx) -> None:
    specs = build_action_tools(ctx)
    names = {s.name for s in specs}
    assert names == {
        "pause_marketplace",
        "unpause_marketplace",
        "discover_seeds",
        "hunt_amazon",
        "hunt_mercadolibre",
        "dispatch_outbox",
        "revalidate_offer",
    }


def test_action_tools_have_additional_properties_false(ctx) -> None:
    for spec in build_action_tools(ctx):
        d = spec.descriptor()
        assert d.inputSchema.get("additionalProperties") is False, spec.name
