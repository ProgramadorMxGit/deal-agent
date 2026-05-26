"""Tests para `ofertas_hunter.mcp.audit`."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ofertas_hunter.db import connect, init_db
from ofertas_hunter.mcp.audit import (
    audit_after,
    audit_before,
    audit_error,
    sanitize,
)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    init_db(db_path)
    c = connect(db_path)
    yield c
    c.close()


def _events(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT kind, severity, payload_json FROM runtime_events "
        "WHERE kind='mcp_tool_called' ORDER BY id"
    ).fetchall()
    return [
        {"kind": r["kind"], "severity": r["severity"],
         "payload": json.loads(r["payload_json"])}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# sanitize
# ---------------------------------------------------------------------------


def test_sanitize_redacts_secret_keys() -> None:
    raw = {
        "cookies": "session=abcd; secure",
        "api_key": "sk-ant-12345",
        "authorization": "Bearer xyz",
        "TOKEN": "abc",
        "password": "p@ss",
        "url": "https://example.com",
    }
    out = sanitize(raw)
    assert out["cookies"] == "[redacted]"
    assert out["api_key"] == "[redacted]"
    assert out["authorization"] == "[redacted]"
    assert out["TOKEN"] == "[redacted]"
    assert out["password"] == "[redacted]"
    # No secreta: pasa intacta
    assert out["url"] == "https://example.com"


def test_sanitize_truncates_long_strings_to_500_with_suffix() -> None:
    long = "a" * 1000
    out = sanitize(long)
    assert isinstance(out, str)
    assert out.startswith("a" * 500)
    assert "+500" in out


def test_sanitize_recurses_into_lists_and_dicts() -> None:
    nested = {"items": [{"cookies": "x"}, {"safe": "y"}]}
    out = sanitize(nested)
    assert out["items"][0]["cookies"] == "[redacted]"
    assert out["items"][1]["safe"] == "y"


def test_sanitize_handles_none_and_primitives() -> None:
    assert sanitize(None) is None
    assert sanitize(42) == 42
    assert sanitize(True) is True
    assert sanitize(3.14) == 3.14


# ---------------------------------------------------------------------------
# audit_before / after / error
# ---------------------------------------------------------------------------


def test_audit_before_emits_runtime_event_info(conn) -> None:
    audit_before(conn, "get_status", {"limit": 5})
    events = _events(conn)
    assert len(events) == 1
    assert events[0]["severity"] == "info"
    assert events[0]["payload"]["phase"] == "before"
    assert events[0]["payload"]["tool"] == "get_status"
    assert events[0]["payload"]["args_summary"] == {"limit": 5}


def test_audit_after_emits_runtime_event_info_with_result_summary(conn) -> None:
    audit_after(conn, "get_status", {"schedule_mode": "active"})
    events = _events(conn)
    assert len(events) == 1
    assert events[0]["payload"]["phase"] == "after"
    assert events[0]["payload"]["result_summary"] == {"schedule_mode": "active"}


def test_audit_error_emits_runtime_event_with_severity_error_and_exception_class(
    conn,
) -> None:
    try:
        raise ValueError("bad input")
    except ValueError as exc:
        audit_error(conn, "hunt_amazon", {"limit": 5}, exc)
    events = _events(conn)
    assert len(events) == 1
    assert events[0]["severity"] == "error"
    assert events[0]["payload"]["phase"] == "error"
    assert events[0]["payload"]["exception_class"] == "ValueError"
    assert events[0]["payload"]["exception_message"] == "bad input"


def test_audit_sanitizes_secrets_in_args(conn) -> None:
    audit_before(conn, "x", {"cookies": "secret123"})
    events = _events(conn)
    assert events[0]["payload"]["args_summary"]["cookies"] == "[redacted]"
