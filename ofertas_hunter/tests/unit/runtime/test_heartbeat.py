"""Tests del AgentRunRegistry."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from ofertas_hunter.db import connect, init_db
from ofertas_hunter.runtime.heartbeat import AgentRunRegistry


def test_start_and_finish_run(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    reg = AgentRunRegistry(conn)
    handle = reg.start("amazon_hunter", summary={"seeds": 5})
    assert handle.run_id > 0
    assert handle.agent_name == "amazon_hunter"

    latest = reg.latest_run("amazon_hunter")
    assert latest is not None
    assert latest["status"] == "running"
    assert latest["ended_at"] is None
    assert latest["last_heartbeat"] is not None
    assert json.loads(latest["summary_json"])["seeds"] == 5

    reg.finish(handle, status="ok", summary={"items": 10})
    latest = reg.latest_run("amazon_hunter")
    assert latest["status"] == "ok"
    assert latest["ended_at"] is not None
    summary = json.loads(latest["summary_json"])
    assert summary["seeds"] == 5
    assert summary["items"] == 10

    conn.close()


def test_heartbeat_updates_last_heartbeat(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    reg = AgentRunRegistry(conn)
    handle = reg.start("amazon_hunter")
    first = reg.last_heartbeat("amazon_hunter")
    assert first is not None
    assert isinstance(first, datetime)

    # Heartbeat actualiza
    reg.heartbeat(handle)
    second = reg.last_heartbeat("amazon_hunter")
    assert second is not None
    assert second >= first

    conn.close()


def test_finish_invalid_status_raises(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    reg = AgentRunRegistry(conn)
    handle = reg.start("x")
    with pytest.raises(ValueError):
        reg.finish(handle, status="foo")
    conn.close()


def test_last_heartbeat_returns_none_for_unknown_agent(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    reg = AgentRunRegistry(conn)
    assert reg.last_heartbeat("nonexistent") is None
    conn.close()


def test_last_heartbeat_only_active_runs(tmp_path: Path):
    """Sólo cuenta corridas que aún no terminaron."""
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    reg = AgentRunRegistry(conn)
    h1 = reg.start("amazon_hunter")
    reg.heartbeat(h1)
    reg.finish(h1, status="ok")

    # Después de finish, last_heartbeat debe devolver None (no hay corrida activa).
    assert reg.last_heartbeat("amazon_hunter") is None
    conn.close()
