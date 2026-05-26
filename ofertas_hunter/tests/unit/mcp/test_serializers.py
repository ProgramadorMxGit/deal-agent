"""Tests para `ofertas_hunter.mcp.serializers`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, time, timezone
from decimal import Decimal

from ofertas_hunter.mcp.serializers import (
    serialize_offer_row,
    serialize_outbox_item,
    serialize_outbox_row,
    serialize_publish_outcome,
    serialize_revalidation,
    serialize_runtime_event_row,
    serialize_schedule_decision,
)
from ofertas_hunter.models import OutboxItem
from ofertas_hunter.runtime.scheduler import ModeDecision, ScheduleMode


def test_serialize_outbox_item_round_trips_payload() -> None:
    item = OutboxItem(
        id=42,
        offer_id=7,
        type="normal",
        message_payload={"title": "JBL", "current_price": 388.0},
        enqueued_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        attempts=0,
        state="pending",
    )
    out = serialize_outbox_item(item)
    assert out["id"] == 42
    assert out["payload"] == {"title": "JBL", "current_price": 388.0}
    assert out["enqueued_at"].endswith("Z")
    assert out["scheduled_for"] is None


def test_serialize_outbox_row_parses_payload_json() -> None:
    row = {
        "id": 1,
        "offer_id": 1,
        "type": "normal",
        "state": "pending",
        "enqueued_at": "2026-01-01T00:00:00Z",
        "scheduled_for": None,
        "attempts": 0,
        "last_attempt_at": None,
        "message_payload_json": '{"title": "x"}',
    }
    out = serialize_outbox_row(row)
    assert out["payload"] == {"title": "x"}


def test_serialize_offer_row_parses_reasons_json() -> None:
    row = {
        "id": 1,
        "product_id": 10,
        "classification": "no_price_error",
        "score": 0,
        "discount_percent": Decimal("56.0"),
        "state": "eligible",
        "reasons_json": '["discount_50_percent"]',
        "current_price_observation_id": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    out = serialize_offer_row(row)
    assert out["reasons"] == ["discount_50_percent"]
    assert out["discount_percent"] == 56.0  # Decimal → float


def test_serialize_runtime_event_row_parses_payload() -> None:
    row = {
        "id": 1,
        "kind": "mcp_tool_called",
        "severity": "info",
        "payload_json": '{"tool": "get_status"}',
        "created_at": "2026-01-01T00:00:00Z",
        "acknowledged_at": None,
    }
    out = serialize_runtime_event_row(row)
    assert out["payload"] == {"tool": "get_status"}
    assert out["severity"] == "info"


def test_iso_returns_z_suffix_for_utc() -> None:
    item = OutboxItem(
        offer_id=1,
        type="normal",
        message_payload={},
        enqueued_at=datetime(2026, 5, 25, 18, 30, 0, tzinfo=timezone.utc),
    )
    out = serialize_outbox_item(item)
    assert out["enqueued_at"].endswith("Z")
    assert "+00:00" not in out["enqueued_at"]


def test_serialize_schedule_decision_carries_next_change_seconds() -> None:
    decision = ModeDecision(
        mode=ScheduleMode.ACTIVE,
        local_time=time(18, 30, 0),
        next_change_in=timedelta(hours=5, minutes=14),
        next_mode=ScheduleMode.HIBERNATING,
    )
    out = serialize_schedule_decision(decision)
    assert out["mode"] == "active"
    assert out["next_mode"] == "hibernating"
    assert out["next_change_in_seconds"] == 5 * 3600 + 14 * 60
    assert out["local_time"] == "18:30:00"


def test_serialize_publish_outcome_with_dry_run() -> None:
    @dataclass
    class FakeFmt:
        type: str = "normal"
        image_url: str = "https://x"
        text: str = "hola"

    @dataclass
    class FakeEvo:
        success: bool = True
        dry_run: bool = True
        status_code: int = 0
        error: str = None

    @dataclass
    class FakeOutcome:
        success: bool = True
        dry_run: bool = True
        formatted: object = None
        evolution_response: object = None
        skipped: bool = False
        skip_reason: str = None
        error: str = None

    outcome = FakeOutcome(formatted=FakeFmt(), evolution_response=FakeEvo())
    out = serialize_publish_outcome(outcome)
    assert out["success"] is True
    assert out["dry_run"] is True
    assert out["formatted"]["type"] == "normal"
    assert out["evolution"]["dry_run"] is True


def test_serialize_revalidation_handles_minimal_detail() -> None:
    @dataclass
    class FakeDetail:
        ok: bool = True
        classification: str = "normal_offer"
        fatal_reason: str = None
        reasons: list = None
        extracted: object = None
        snapshot_saved: int = None
        suggested_outbox_type: str = "normal"
        confidence_label: str = "low"

    out = serialize_revalidation(FakeDetail(reasons=["x"]))
    assert out["ok"] is True
    assert out["reasons"] == ["x"]
    assert out["extracted"] is None
