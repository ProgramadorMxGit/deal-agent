"""Tests de la tool MCP `enrich_amazon_affiliates`."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ofertas_hunter.config import Settings
from ofertas_hunter.db import connect, init_db
from ofertas_hunter.mcp.context import ServerContext
from ofertas_hunter.mcp.server import MCPServer
from ofertas_hunter.mcp.tools.action_tools import build_action_tools
from ofertas_hunter.runtime.scheduler import ModeDecision, ScheduleMode
from datetime import time, timedelta


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


@dataclass
class _FakeReport:
    total_candidates: int = 3
    enriched: int = 2
    failed: int = 1
    skipped: int = 0
    outcomes: list = field(default_factory=list)


@dataclass
class _FakeEnricher:
    calls: int = 0
    last_limit: int = 0

    async def run(self, *, limit: int = 20):
        self.calls += 1
        self.last_limit = limit
        return _FakeReport()


@pytest.fixture
def db(tmp_path: Path):
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


@pytest.mark.asyncio
async def test_enrich_amazon_affiliates_calls_enricher(server, ctx):
    enricher = _FakeEnricher()

    async def _get():
        return enricher

    ctx.get_amazon_affiliate_enricher = _get

    out = await server.dispatch("enrich_amazon_affiliates", {"limit": 5})

    assert out["success"] is True
    assert enricher.calls == 1
    assert enricher.last_limit == 5
    assert out["enriched"] == 2
    assert out["failed"] == 1
    assert out["total_candidates"] == 3


@pytest.mark.asyncio
async def test_enrich_amazon_affiliates_is_registered(server):
    out = await server.dispatch("enrich_amazon_affiliates", {"limit": 2})
    assert "error" not in out or out.get("error") != "unknown_tool"


@pytest.mark.asyncio
async def test_enrich_amazon_affiliates_holds_amazon_lock(server, ctx):
    """El enricher debe correr bajo el lock 'amazon' para NO competir con
    hunt_amazon por el perfil persistente de Chromium (causa de
    sitestripe_not_visible / TargetClosedError)."""

    locked_during_run = {"value": False}

    @dataclass
    class _LockProbingEnricher:
        async def run(self, *, limit: int = 20):
            # El lock 'amazon' debe estar tomado mientras corre el enricher.
            locked_during_run["value"] = ctx.lock_for("amazon").locked()
            return _FakeReport()

    async def _get():
        return _LockProbingEnricher()

    ctx.get_amazon_affiliate_enricher = _get

    out = await server.dispatch("enrich_amazon_affiliates", {"limit": 2})

    assert out["success"] is True
    assert locked_during_run["value"] is True, (
        "el enricher debe ejecutarse con el lock 'amazon' tomado"
    )
    # Tras terminar, el lock debe quedar liberado.
    assert ctx.lock_for("amazon").locked() is False


@pytest.mark.asyncio
async def test_enrich_skips_when_profile_lock_busy(server, ctx, tmp_path):
    """Si el lock de archivo del perfil está ocupado (otro proceso usando
    Chromium), el enricher NO debe abrir Chromium: retorna skip y NO llama
    al enricher."""
    from ofertas_hunter.runtime.profile_lock import ProfileLock

    lock_path = str(tmp_path / "amazon.profile.lock")
    ctx.amazon_profile_lock_path = lock_path

    called = {"value": False}

    @dataclass
    class _NeverCalledEnricher:
        async def run(self, *, limit: int = 20):
            called["value"] = True
            return _FakeReport()

    async def _get():
        return _NeverCalledEnricher()

    ctx.get_amazon_affiliate_enricher = _get

    # Ocupar el lock desde "otro proceso" (otro handle).
    holder = ProfileLock(lock_path)
    assert holder.try_acquire() is True
    try:
        out = await server.dispatch("enrich_amazon_affiliates", {"limit": 2})
    finally:
        holder.release()

    assert out["success"] is False
    assert out.get("skipped") is True
    assert out.get("reason") == "amazon_profile_busy"
    assert called["value"] is False, "no debe abrir Chromium si el perfil está ocupado"


@pytest.mark.asyncio
async def test_enrich_releases_profile_lock_on_failure(server, ctx, tmp_path):
    """Si el enricher lanza, el lock de archivo debe quedar liberado."""
    from ofertas_hunter.runtime.profile_lock import ProfileLock

    lock_path = str(tmp_path / "amazon.profile.lock")
    ctx.amazon_profile_lock_path = lock_path

    @dataclass
    class _BoomEnricher:
        async def run(self, *, limit: int = 20):
            raise RuntimeError("boom during modal")

    async def _get():
        return _BoomEnricher()

    ctx.get_amazon_affiliate_enricher = _get

    out = await server.dispatch("enrich_amazon_affiliates", {"limit": 2})
    assert out["success"] is False
    assert out.get("error") == "enrich_failed"

    # El lock debe estar libre: otro handle puede tomarlo.
    other = ProfileLock(lock_path)
    assert other.try_acquire() is True
    other.release()


