"""Tests para `MCPServer.dispatch`.

Cubre el flujo:
    unknown tool → validation_failed → safety skip → handler exception → success.
Cada caso emite los runtime_events correctos.
"""

from __future__ import annotations

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
from ofertas_hunter.mcp.tools import ToolSpec
from ofertas_hunter.runtime.scheduler import ModeDecision, ScheduleMode


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


def _events(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        "SELECT kind, severity, payload_json FROM runtime_events "
        "WHERE kind='mcp_tool_called' ORDER BY id"
    ).fetchall()
    return [
        {"severity": r["severity"], "payload": json.loads(r["payload_json"])}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Specs de prueba
# ---------------------------------------------------------------------------


async def _ok_handler(ctx, args):
    return {"echo": args.get("x", "default")}


async def _raises_handler(ctx, args):
    raise RuntimeError("boom")


def _spec_ok() -> ToolSpec:
    return ToolSpec(
        name="probe_ok",
        description="probe",
        input_schema={
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "additionalProperties": False,
        },
        handler=_ok_handler,
        is_read_only=True,
    )


def _spec_with_safety() -> ToolSpec:
    return ToolSpec(
        name="probe_action",
        description="probe with safety",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_ok_handler,
        is_read_only=False,
        safety_rules=("schedule_authority",),
    )


def _spec_raises() -> ToolSpec:
    return ToolSpec(
        name="probe_raises",
        description="probe raises",
        input_schema={"type": "object", "additionalProperties": False},
        handler=_raises_handler,
        is_read_only=True,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_error(ctx) -> None:
    server = MCPServer(ctx, registry={})
    out = await server.dispatch("nonexistent")
    assert out == {"error": "unknown_tool", "tool": "nonexistent"}


@pytest.mark.asyncio
async def test_dispatch_validation_error_emits_audit_error_event(ctx) -> None:
    spec = _spec_ok()
    server = MCPServer(ctx, registry={"probe_ok": spec})
    out = await server.dispatch("probe_ok", {"unknown_field": 1})
    assert out["error"] == "validation_failed"
    events = _events(ctx.db)
    assert any(e["severity"] == "error" for e in events)


@pytest.mark.asyncio
async def test_dispatch_handler_exception_returns_handler_failed_with_class_name(ctx) -> None:
    spec = _spec_raises()
    server = MCPServer(ctx, registry={"probe_raises": spec})
    out = await server.dispatch("probe_raises", {})
    assert out == {"error": "handler_failed", "exception_class": "RuntimeError"}


@pytest.mark.asyncio
async def test_dispatch_handler_exception_emits_audit_error_event(ctx) -> None:
    spec = _spec_raises()
    server = MCPServer(ctx, registry={"probe_raises": spec})
    await server.dispatch("probe_raises", {})
    events = _events(ctx.db)
    # Tiene que haber al menos before(info) + error(error)
    severities = [e["severity"] for e in events]
    assert "error" in severities
    err = [e for e in events if e["severity"] == "error"][0]
    assert err["payload"]["exception_class"] == "RuntimeError"


@pytest.mark.asyncio
async def test_dispatch_skip_from_safety_returns_skipped_payload(ctx) -> None:
    # Forzamos scheduler en hibernating monkey-patcheando ctx.scheduler
    class _SchedHibernate:
        def decide(self):
            return ModeDecision(
                mode=ScheduleMode.HIBERNATING,
                local_time=time(23, 45),
                next_change_in=timedelta(hours=7),
                next_mode=ScheduleMode.WARMUP,
            )

    ctx.scheduler = _SchedHibernate()
    spec = _spec_with_safety()
    server = MCPServer(ctx, registry={"probe_action": spec})
    out = await server.dispatch("probe_action", {})
    assert out["skipped"] is True
    assert out["reason"] == "hibernating"


@pytest.mark.asyncio
async def test_dispatch_success_emits_two_audit_events_before_and_after(ctx) -> None:
    spec = _spec_ok()
    server = MCPServer(ctx, registry={"probe_ok": spec})
    out = await server.dispatch("probe_ok", {"x": "hi"})
    assert out == {"echo": "hi"}
    events = _events(ctx.db)
    phases = [e["payload"]["phase"] for e in events]
    assert "before" in phases
    assert "after" in phases


@pytest.mark.asyncio
async def test_dispatch_handler_returning_non_dict_is_wrapped(ctx) -> None:
    async def handler(ctx, args):
        return [1, 2, 3]

    spec = ToolSpec(
        name="probe_list",
        description="returns list",
        input_schema={"type": "object", "additionalProperties": False},
        handler=handler,
        is_read_only=True,
    )
    server = MCPServer(ctx, registry={"probe_list": spec})
    out = await server.dispatch("probe_list", {})
    assert out == {"data": [1, 2, 3]}


@pytest.mark.asyncio
async def test_register_tools_returns_descriptors_with_additional_properties_false(ctx) -> None:
    spec = _spec_ok()
    server = MCPServer(ctx, registry={"probe_ok": spec})
    # Smoke: descriptor() devuelve un Tool del SDK
    descriptor = list(server.registry.values())[0].descriptor()
    assert descriptor.name == "probe_ok"
    assert descriptor.inputSchema["additionalProperties"] is False
