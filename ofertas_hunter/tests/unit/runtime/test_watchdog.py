"""Tests del RuntimeWatchdog."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ofertas_hunter.db import connect, init_db
from ofertas_hunter.runtime.heartbeat import AgentRunHandle, AgentRunRegistry
from ofertas_hunter.runtime.watchdog import (
    RuntimeWatchdog,
    WatchdogConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


T0 = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    """Clock inyectable que avanza manualmente en los tests."""

    def __init__(self, start: datetime = T0):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


async def _wait_done(task: asyncio.Task, timeout: float = 1.0) -> None:
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        pass


def _watchdog(conn, *, clock: Clock, **overrides):
    config = WatchdogConfig(
        poll_interval_seconds=0.01,
        stale_after_seconds=overrides.get("stale_after_seconds", 60.0),
        backoff_seconds=overrides.get("backoff_seconds", (0, 0, 0)),
        max_restarts_in_window=overrides.get("max_restarts_in_window", 3),
        restart_window_seconds=overrides.get("restart_window_seconds", 600.0),
    )
    return RuntimeWatchdog(conn, config=config, clock=clock)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_registers_agent(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    clock = Clock()
    wd = _watchdog(conn, clock=clock)

    async def factory(handle: AgentRunHandle, registry):
        await asyncio.sleep(0)

    wd.register("amazon_hunter", factory)
    assert "amazon_hunter" in wd.agents

    with pytest.raises(ValueError):
        wd.register("amazon_hunter", factory)
    conn.close()


@pytest.mark.asyncio
async def test_watchdog_starts_agent_and_heartbeat_keeps_alive(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    clock = Clock()
    wd = _watchdog(conn, clock=clock, stale_after_seconds=30)

    async def factory(handle: AgentRunHandle, registry):
        # Heartbeat continuo
        for _ in range(20):
            registry.heartbeat(handle)
            await asyncio.sleep(0)

    wd.register("amazon_hunter", factory)
    await wd.start_all()

    # No avanzamos el clock; el agente debería seguir vivo y NO reiniciarse.
    await wd.tick()
    agent = wd.get("amazon_hunter")
    assert agent.restart_count == 0
    assert agent.degraded is False

    await _wait_done(agent.task)
    conn.close()


@pytest.mark.asyncio
async def test_watchdog_restarts_agent_when_heartbeat_stale(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    clock = Clock()
    wd = _watchdog(conn, clock=clock, stale_after_seconds=30)

    spawn_count = {"n": 0}

    async def factory(handle: AgentRunHandle, registry):
        spawn_count["n"] += 1
        # Sólo emite heartbeat la primera vez.
        # En la segunda spawn, también heartbeat, así no degrada por encadenado.
        registry.heartbeat(handle)
        # Mantener el task vivo pero sin más heartbeats.
        await asyncio.sleep(10)

    wd.register("ml_hunter", factory)
    await wd.start_all()

    # Permitir que el factory ejecute al menos el primer heartbeat.
    await asyncio.sleep(0.05)

    # Avanzar el clock más allá del stale.
    clock.advance(120)
    await wd.tick()
    await asyncio.sleep(0.05)

    agent = wd.get("ml_hunter")
    assert agent.restart_count >= 1
    assert agent.degraded is False
    assert spawn_count["n"] >= 2  # se respawneó

    # Hay un runtime_event de tipo agent_restart
    rows = conn.execute(
        "SELECT kind, severity FROM runtime_events"
    ).fetchall()
    assert any(r["kind"] == "agent_restart" and r["severity"] == "warning" for r in rows)

    await wd.shutdown_all()
    conn.close()


@pytest.mark.asyncio
async def test_watchdog_restarts_agent_when_task_crashes(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    clock = Clock()
    wd = _watchdog(conn, clock=clock, stale_after_seconds=999)

    spawn_count = {"n": 0}

    async def factory(handle: AgentRunHandle, registry):
        spawn_count["n"] += 1
        registry.heartbeat(handle)
        if spawn_count["n"] < 3:
            raise RuntimeError(f"boom #{spawn_count['n']}")

    wd.register("flappy", factory)
    await wd.start_all()
    await asyncio.sleep(0.05)

    # Tick: el task ya está done con error → debe reiniciar.
    await wd.tick()
    await asyncio.sleep(0.05)
    await wd.tick()
    await asyncio.sleep(0.05)

    agent = wd.get("flappy")
    assert agent.restart_count >= 2
    # El último spawn (#3) NO debe lanzar, así que el task termina ok.
    rows = conn.execute(
        "SELECT status FROM agent_runs WHERE agent_name='flappy' ORDER BY id"
    ).fetchall()
    assert any(r["status"] == "error" for r in rows)
    assert any(r["status"] == "ok" for r in rows)

    await wd.shutdown_all()
    conn.close()


@pytest.mark.asyncio
async def test_watchdog_degrades_agent_after_too_many_restarts(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    clock = Clock()
    wd = _watchdog(
        conn,
        clock=clock,
        stale_after_seconds=999,
        max_restarts_in_window=2,
        restart_window_seconds=600,
    )

    async def factory(handle: AgentRunHandle, registry):
        registry.heartbeat(handle)
        raise RuntimeError("always crash")

    wd.register("doomed", factory)
    await wd.start_all()
    await asyncio.sleep(0.05)

    # Disparamos varios ticks; cada uno reinicia el task que vuelve a fallar.
    for _ in range(5):
        await wd.tick()
        await asyncio.sleep(0.02)

    agent = wd.get("doomed")
    assert agent.degraded is True
    assert "too_many_restarts" in (agent.last_failure_reason or "")

    rows = conn.execute(
        "SELECT kind, severity, payload_json FROM runtime_events"
    ).fetchall()
    assert any(
        r["kind"] == "agent_degraded" and r["severity"] == "critical" for r in rows
    )

    # Tick adicional NO debe re-respawnear un agente degradado.
    pre_count = agent.restart_count
    await wd.tick()
    assert agent.restart_count == pre_count

    await wd.shutdown_all()
    conn.close()


@pytest.mark.asyncio
async def test_watchdog_finish_recorded_with_status(tmp_path: Path):
    """Cuando un task termina normalmente, agent_runs queda con status=ok."""
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    clock = Clock()
    wd = _watchdog(conn, clock=clock)

    async def factory(handle: AgentRunHandle, registry):
        registry.heartbeat(handle)
        return  # termina ok

    wd.register("oneshot", factory)
    await wd.start_all()
    await asyncio.sleep(0.05)

    rows = conn.execute(
        "SELECT status, ended_at FROM agent_runs WHERE agent_name='oneshot' ORDER BY id"
    ).fetchall()
    assert rows[0]["status"] == "ok"
    assert rows[0]["ended_at"] is not None
    await wd.shutdown_all()
    conn.close()


@pytest.mark.asyncio
async def test_watchdog_shutdown_cancels_running_tasks(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    clock = Clock()
    wd = _watchdog(conn, clock=clock)

    async def factory(handle: AgentRunHandle, registry):
        registry.heartbeat(handle)
        # task largo
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise

    wd.register("longrunning", factory)
    await wd.start_all()
    await asyncio.sleep(0.02)

    await wd.shutdown_all()
    agent = wd.get("longrunning")
    assert agent.task.done()
    rows = conn.execute(
        "SELECT status FROM agent_runs WHERE agent_name='longrunning' ORDER BY id"
    ).fetchall()
    assert rows[0]["status"] == "killed"
    conn.close()
