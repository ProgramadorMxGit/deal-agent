"""Tests del Orchestrator con builder de fakes (sin Playwright/Telethon)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

import pytest

from ofertas_hunter.config import Settings
from ofertas_hunter.db import connect, init_db
from ofertas_hunter.dispatching.cooldown import CooldownPolicy
from ofertas_hunter.dispatching.outbox import OutboxConfig, SqliteOutbox
from ofertas_hunter.models import OutboxItem, OutboxType
from ofertas_hunter.orchestrator import (
    AgentFactoryBuilder,
    AgentFactoryFn,
    Orchestrator,
    OrchestratorConfig,
)
from ofertas_hunter.runtime.heartbeat import AgentRunHandle, AgentRunRegistry


# ---------------------------------------------------------------------------
# Builder de fakes
# ---------------------------------------------------------------------------


class FakeFactoryBuilder(AgentFactoryBuilder):
    """Builder que devuelve factories rapiditas, sin red ni Playwright."""

    def __init__(self, conn, settings: Settings, config: OrchestratorConfig):
        super().__init__(conn, settings, config)
        self.calls: dict[str, int] = {}
        self.fail_agent: Optional[str] = None

    def _make_simple(self, name: str) -> AgentFactoryFn:
        async def factory(handle: AgentRunHandle, registry: AgentRunRegistry) -> None:
            registry.heartbeat(handle)
            self.calls[name] = self.calls.get(name, 0) + 1
            if self.fail_agent == name:
                raise RuntimeError(f"forced failure: {name}")
            registry.heartbeat(handle)

        return factory

    def make_dispatcher_factory(self) -> AgentFactoryFn:
        return self._make_simple("dispatcher")

    def make_amazon_hunter_factory(self) -> Optional[AgentFactoryFn]:
        if not self.settings.amazon_enabled:
            self._emit_skip("amazon_hunter", "amazon_disabled")
            return None
        return self._make_simple("amazon_hunter")

    def make_mercadolibre_hunter_factory(self) -> Optional[AgentFactoryFn]:
        if not self.settings.mercadolibre_enabled:
            self._emit_skip("mercadolibre_hunter", "mercadolibre_disabled")
            return None
        return self._make_simple("mercadolibre_hunter")

    def make_telegram_listener_factory(self) -> Optional[AgentFactoryFn]:
        if not self.settings.telegram_enabled:
            self._emit_skip("telegram_listener", "telegram_disabled")
            return None
        return self._make_simple("telegram_listener")

    def make_maintenance_factory(self) -> AgentFactoryFn:
        return self._make_simple("maintenance")


def _settings(**overrides) -> Settings:
    """Crea Settings con env aislado para cada test."""
    base = {
        "amazon_enabled": False,
        "mercadolibre_enabled": False,
        "telegram_enabled": False,
        "publishing_enabled": False,
        "publishing_dry_run": True,
    }
    base.update(overrides)
    return Settings(**base)


def _config(**overrides) -> OrchestratorConfig:
    return OrchestratorConfig(once=True, **overrides)


def _seed_offer_graph(conn) -> int:
    conn.execute(
        """
        INSERT INTO products(
            marketplace, url_canonical, title, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            "amazon",
            "https://example.com/p",
            "Producto",
            "2026-05-25T12:00:00.000Z",
            "2026-05-25T12:00:00.000Z",
        ),
    )
    product_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.execute(
        """
        INSERT INTO price_observations(
            product_id, current_price, previous_price, source, raw_signals_json, observed_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            product_id,
            100,
            200,
            "amazon_hunter",
            "{}",
            "2026-05-25T12:00:00.000Z",
        ),
    )
    price_obs_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.execute(
        """
        INSERT INTO offers(
            product_id, current_price_observation_id, classification, score,
            reasons_json, discount_percent, state, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            product_id,
            price_obs_id,
            "normal_offer",
            90,
            "[]",
            50,
            "eligible",
            "2026-05-25T12:00:00.000Z",
            "2026-05-25T12:00:00.000Z",
        ),
    )
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_registers_only_enabled_agents(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    settings = _settings(amazon_enabled=False, mercadolibre_enabled=False, telegram_enabled=False)
    config = _config()
    builder = FakeFactoryBuilder(conn, settings, config)
    orch = Orchestrator(conn, settings=settings, config=config, builder=builder)

    report = orch.register_agents()
    # Sólo dispatcher + maintenance.
    assert "outbox_dispatcher" in report.registered
    assert "maintenance" in report.registered
    assert "amazon_hunter" not in report.registered
    assert "mercadolibre_hunter" not in report.registered
    assert "telegram_listener" not in report.registered
    skipped_names = {a for a, _ in report.skipped}
    assert "amazon_hunter" in skipped_names
    assert "mercadolibre_hunter" in skipped_names
    assert "telegram_listener" in skipped_names

    # Hay runtime_events de skip.
    rows = conn.execute(
        "SELECT kind, payload_json FROM runtime_events WHERE kind = 'agent_skipped'"
    ).fetchall()
    skipped_in_db = [json.loads(r["payload_json"])["agent"] for r in rows]
    assert "amazon_hunter" in skipped_in_db
    conn.close()


@pytest.mark.asyncio
async def test_orchestrator_registers_all_when_flags_enabled(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    settings = _settings(amazon_enabled=True, mercadolibre_enabled=True, telegram_enabled=True)
    config = _config()
    builder = FakeFactoryBuilder(conn, settings, config)
    orch = Orchestrator(conn, settings=settings, config=config, builder=builder)

    report = orch.register_agents()
    assert set(report.registered) == {
        "maintenance",
        "outbox_dispatcher",
        "amazon_hunter",
        "mercadolibre_hunter",
        "telegram_listener",
    }
    conn.close()


@pytest.mark.asyncio
async def test_orchestrator_run_once_calls_each_agent(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    settings = _settings(amazon_enabled=True, mercadolibre_enabled=False, telegram_enabled=False)
    config = _config()
    builder = FakeFactoryBuilder(conn, settings, config)
    orch = Orchestrator(conn, settings=settings, config=config, builder=builder)

    await orch.run()

    # Cada agente registrado se ejecutó al menos una vez.
    assert builder.calls["dispatcher"] >= 1
    assert builder.calls["amazon_hunter"] >= 1
    assert builder.calls["maintenance"] >= 1

    # Y el evento de inicio se emitió.
    rows = conn.execute(
        "SELECT kind FROM runtime_events WHERE kind = 'orchestrator_starting'"
    ).fetchall()
    assert len(rows) == 1
    conn.close()


@pytest.mark.asyncio
async def test_orchestrator_handles_agent_failure_gracefully(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    settings = _settings(amazon_enabled=True)
    config = _config()
    builder = FakeFactoryBuilder(conn, settings, config)
    builder.fail_agent = "amazon_hunter"

    orch = Orchestrator(conn, settings=settings, config=config, builder=builder)

    await orch.run()

    # Dispatcher y maintenance siguieron corriendo aunque amazon_hunter haya fallado.
    assert builder.calls["dispatcher"] >= 1
    assert builder.calls["maintenance"] >= 1

    # agent_runs registró el error.
    rows = conn.execute(
        "SELECT status, summary_json FROM agent_runs "
        "WHERE agent_name='amazon_hunter' ORDER BY id DESC LIMIT 1"
    ).fetchall()
    assert rows
    assert rows[0]["status"] == "error"
    summary = json.loads(rows[0]["summary_json"])
    assert "error" in summary
    conn.close()


@pytest.mark.asyncio
async def test_orchestrator_emits_event_with_skipped_agents(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    settings = _settings(amazon_enabled=False)
    config = _config()
    builder = FakeFactoryBuilder(conn, settings, config)
    orch = Orchestrator(conn, settings=settings, config=config, builder=builder)

    await orch.run()

    rows = conn.execute(
        "SELECT payload_json FROM runtime_events "
        "WHERE kind = 'orchestrator_starting'"
    ).fetchall()
    assert rows
    payload = json.loads(rows[0]["payload_json"])
    skipped = [s["agent"] for s in payload["skipped"]]
    assert "amazon_hunter" in skipped
    conn.close()


@pytest.mark.asyncio
async def test_orchestrator_dispatcher_always_registered(tmp_path: Path):
    """Aún con todos los flags en false, dispatcher + maintenance se registran."""
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    settings = _settings()
    config = _config()
    builder = FakeFactoryBuilder(conn, settings, config)
    orch = Orchestrator(conn, settings=settings, config=config, builder=builder)
    report = orch.register_agents()

    assert "outbox_dispatcher" in report.registered
    assert "maintenance" in report.registered
    conn.close()


@pytest.mark.asyncio
async def test_orchestrator_records_agent_runs_for_each_cycle(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    settings = _settings(mercadolibre_enabled=True)
    config = _config()
    builder = FakeFactoryBuilder(conn, settings, config)
    orch = Orchestrator(conn, settings=settings, config=config, builder=builder)
    await orch.run()

    rows = conn.execute(
        "SELECT agent_name, status FROM agent_runs ORDER BY id"
    ).fetchall()
    names = [r["agent_name"] for r in rows]
    assert "outbox_dispatcher" in names
    assert "maintenance" in names
    assert "mercadolibre_hunter" in names
    # Todos terminaron ok (es --once con factories triviales).
    statuses = {r["status"] for r in rows}
    assert statuses == {"ok"}
    conn.close()


@pytest.mark.asyncio
async def test_dispatcher_factory_lazy_revalidator_uses_builder_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    settings = _settings(
        publishing_enabled=False,
        publishing_dry_run=True,
    )
    config = _config(dispatcher_loop_interval=0.01)

    outbox = SqliteOutbox(
        conn,
        OutboxConfig(
            revalidate_age_seconds=3600,
            cooldown=CooldownPolicy(cooldown_seconds=300),
        ),
    )
    offer_id = _seed_offer_graph(conn)
    outbox.enqueue(
        OutboxItem(
            offer_id=offer_id,
            type=OutboxType.NORMAL.value,
            enqueued_at=datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc),
            message_payload={
                "title": "Producto viejo",
                "url": "https://amzn.to/x",
                "image_url": "https://img/x.jpg",
                "current_price": 100,
                "previous_price": 200,
                "discount_percent": 50,
                "requires_live_validation": True,
            },
        )
    )

    captured: dict[str, object] = {}

    class FakeBrowserConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeBrowserWorker:
        def __init__(self, config):
            self.config = config

        async def _ensure_started(self):
            return None

        async def aclose(self):
            return None

    class FakeRevalidator:
        def __init__(self, *, browser, db_conn):
            captured["browser"] = browser
            captured["db_conn"] = db_conn

        async def revalidate(self, item):
            from ofertas_hunter.dispatching.dispatcher import RevalidationResult

            return RevalidationResult(still_eligible=True, payload=item.message_payload)

    monkeypatch.setitem(
        sys.modules,
        "ofertas_hunter.browser.browser_context",
        types.SimpleNamespace(BrowserConfig=FakeBrowserConfig),
    )
    monkeypatch.setitem(
        sys.modules,
        "ofertas_hunter.browser.playwright_worker",
        types.SimpleNamespace(PlaywrightBrowserWorker=FakeBrowserWorker),
    )
    monkeypatch.setitem(
        sys.modules,
        "ofertas_hunter.revalidation.playwright_revalidator",
        types.SimpleNamespace(PlaywrightRevalidator=FakeRevalidator),
    )

    builder = AgentFactoryBuilder(conn, settings, config)
    registry = AgentRunRegistry(conn)
    handle = registry.start("outbox_dispatcher")

    factory = builder.make_dispatcher_factory()
    await factory(handle, registry)

    assert captured["db_conn"] is conn
    conn.close()
