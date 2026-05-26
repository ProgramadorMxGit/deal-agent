"""Tests para `ofertas_hunter.mcp.safety`."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from ofertas_hunter.db import connect, init_db
from ofertas_hunter.mcp.safety import (
    OK,
    PauseInfo,
    SkipResult,
    apply_hard_rules,
    list_rule_names,
    rule_cooldown_normal,
    rule_image_price_url,
    rule_marketplace_paused,
    rule_ml_affiliate,
    rule_schedule_authority,
    rule_telegram_to_ml,
)
from ofertas_hunter.runtime.scheduler import ModeDecision, ScheduleMode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeSettings:
    whatsapp_cooldown_seconds: int = 300
    mercadolibre_affiliate_required_for_publish: bool = True


@dataclass
class _FakeScheduler:
    mode: ScheduleMode = ScheduleMode.ACTIVE
    next_mode: ScheduleMode = ScheduleMode.HIBERNATING
    next_change: timedelta = timedelta(hours=5)

    def decide(self) -> ModeDecision:
        return ModeDecision(
            mode=self.mode,
            local_time=time(12, 0),
            next_change_in=self.next_change,
            next_mode=self.next_mode,
        )


@dataclass
class _FakeCtx:
    db: sqlite3.Connection
    settings: _FakeSettings = field(default_factory=_FakeSettings)
    scheduler: _FakeScheduler = field(default_factory=_FakeScheduler)
    pause_state: dict[str, PauseInfo] = field(default_factory=dict)
    last_normal_publication_at: Any = None


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "x.db"
    init_db(db_path)
    c = connect(db_path)
    yield c
    c.close()


def _insert_outbox(
    db: sqlite3.Connection, *, type_: str = "normal", payload: dict | None = None
) -> int:
    if payload is None:
        payload = {
            "image_url": "https://x/img.jpg",
            "current_price": 100.0,
            "url": "https://x/p",
        }
    # outbox -> offer -> product (FK chain)
    cur = db.execute(
        "INSERT INTO products (marketplace, marketplace_id, url_canonical, title, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("amazon", "B0X", "https://x/p", "T", "2026-01-01", "2026-01-01"),
    )
    pid = cur.lastrowid
    cur = db.execute(
        "INSERT INTO offers (product_id, classification, score, reasons_json, "
        "state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
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


# ---------------------------------------------------------------------------
# rule_schedule_authority
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_schedule_authority_allows_when_active(db) -> None:
    ctx = _FakeCtx(db=db)
    assert (await rule_schedule_authority(ctx, {})).skipped is False


@pytest.mark.asyncio
async def test_rule_schedule_authority_skips_when_hibernating(db) -> None:
    ctx = _FakeCtx(db=db, scheduler=_FakeScheduler(mode=ScheduleMode.HIBERNATING))
    out = await rule_schedule_authority(ctx, {})
    assert out.skipped is True
    assert out.reason == "hibernating"


@pytest.mark.asyncio
async def test_rule_schedule_authority_skips_when_warmup(db) -> None:
    ctx = _FakeCtx(db=db, scheduler=_FakeScheduler(mode=ScheduleMode.WARMUP))
    out = await rule_schedule_authority(ctx, {})
    assert out.skipped is True
    assert out.reason == "warmup"


# ---------------------------------------------------------------------------
# rule_marketplace_paused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_marketplace_paused_no_pause_returns_ok(db) -> None:
    ctx = _FakeCtx(db=db)
    assert (await rule_marketplace_paused(ctx, {"marketplace": "amazon"})).skipped is False


@pytest.mark.asyncio
async def test_rule_marketplace_paused_skips_with_until_iso(db) -> None:
    until = datetime.now(timezone.utc) + timedelta(minutes=5)
    ctx = _FakeCtx(
        db=db,
        pause_state={"amazon": PauseInfo(active=True, reason="captcha", until=until)},
    )
    out = await rule_marketplace_paused(ctx, {"marketplace": "amazon"})
    assert out.skipped is True
    assert out.reason == "paused"
    assert out.detail["marketplace"] == "amazon"
    assert out.detail["until"] is not None
    assert out.detail["reason_text"] == "captcha"


@pytest.mark.asyncio
async def test_rule_marketplace_paused_clears_when_ttl_expired(db) -> None:
    until = datetime.now(timezone.utc) - timedelta(seconds=1)  # ya pasó
    pause = PauseInfo(active=True, reason="x", until=until)
    ctx = _FakeCtx(db=db, pause_state={"amazon": pause})
    out = await rule_marketplace_paused(ctx, {"marketplace": "amazon"})
    assert out.skipped is False
    assert pause.active is False  # se limpió


# ---------------------------------------------------------------------------
# rule_cooldown_normal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_cooldown_normal_no_previous_publication_is_ok(db) -> None:
    ctx = _FakeCtx(db=db)
    assert (await rule_cooldown_normal(ctx, {})).skipped is False


@pytest.mark.asyncio
async def test_rule_cooldown_normal_skips_when_within_window(db) -> None:
    ctx = _FakeCtx(db=db)
    ctx.last_normal_publication_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    out = await rule_cooldown_normal(ctx, {})
    assert out.skipped is True
    assert out.reason == "cooldown_active"
    assert out.detail["remaining_seconds"] >= 0


@pytest.mark.asyncio
async def test_rule_cooldown_normal_passes_after_window(db) -> None:
    ctx = _FakeCtx(db=db)
    ctx.last_normal_publication_at = datetime.now(timezone.utc) - timedelta(seconds=400)
    assert (await rule_cooldown_normal(ctx, {})).skipped is False


# ---------------------------------------------------------------------------
# rule_image_price_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_image_price_url_returns_first_missing_field_image(db) -> None:
    ctx = _FakeCtx(db=db)
    oid = _insert_outbox(db, payload={"current_price": 1.0, "url": "https://x"})
    out = await rule_image_price_url(ctx, {"outbox_id": oid})
    assert out.skipped is True and out.reason == "missing_image_url"


@pytest.mark.asyncio
async def test_rule_image_price_url_returns_first_missing_field_price(db) -> None:
    ctx = _FakeCtx(db=db)
    oid = _insert_outbox(
        db, payload={"image_url": "https://x", "url": "https://x"}
    )
    out = await rule_image_price_url(ctx, {"outbox_id": oid})
    assert out.skipped is True and out.reason == "missing_current_price"


@pytest.mark.asyncio
async def test_rule_image_price_url_returns_missing_url(db) -> None:
    ctx = _FakeCtx(db=db)
    oid = _insert_outbox(
        db, payload={"image_url": "https://x", "current_price": 1.0}
    )
    out = await rule_image_price_url(ctx, {"outbox_id": oid})
    assert out.skipped is True and out.reason == "missing_url"


@pytest.mark.asyncio
async def test_rule_image_price_url_passes_when_complete(db) -> None:
    ctx = _FakeCtx(db=db)
    oid = _insert_outbox(db)
    out = await rule_image_price_url(ctx, {"outbox_id": oid})
    assert out.skipped is False


# ---------------------------------------------------------------------------
# rule_ml_affiliate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_ml_affiliate_skips_when_required_and_missing(db) -> None:
    ctx = _FakeCtx(db=db)
    oid = _insert_outbox(
        db,
        payload={
            "marketplace": "mercadolibre",
            "image_url": "https://x",
            "current_price": 1.0,
            "url": "https://x",
        },
    )
    out = await rule_ml_affiliate(ctx, {"outbox_id": oid})
    assert out.skipped is True and out.reason == "missing_affiliate_url"


@pytest.mark.asyncio
async def test_rule_ml_affiliate_passes_when_affiliate_present(db) -> None:
    ctx = _FakeCtx(db=db)
    oid = _insert_outbox(
        db,
        payload={
            "marketplace": "mercadolibre",
            "affiliate_url": "https://aff/x",
            "current_price": 1.0,
        },
    )
    out = await rule_ml_affiliate(ctx, {"outbox_id": oid})
    assert out.skipped is False


@pytest.mark.asyncio
async def test_rule_ml_affiliate_no_op_for_amazon(db) -> None:
    ctx = _FakeCtx(db=db)
    oid = _insert_outbox(db, payload={"marketplace": "amazon"})
    assert (await rule_ml_affiliate(ctx, {"outbox_id": oid})).skipped is False


@pytest.mark.asyncio
async def test_rule_ml_affiliate_disabled_in_settings(db) -> None:
    ctx = _FakeCtx(
        db=db,
        settings=_FakeSettings(mercadolibre_affiliate_required_for_publish=False),
    )
    oid = _insert_outbox(db, payload={"marketplace": "mercadolibre"})
    assert (await rule_ml_affiliate(ctx, {"outbox_id": oid})).skipped is False


# ---------------------------------------------------------------------------
# rule_telegram_to_ml
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_telegram_to_ml_blocks_ml_with_telegram_source(db) -> None:
    ctx = _FakeCtx(db=db)
    oid = _insert_outbox(
        db, payload={"marketplace": "mercadolibre", "source": "telegram"}
    )
    out = await rule_telegram_to_ml(ctx, {"outbox_id": oid})
    assert out.skipped is True and out.reason == "telegram_to_ml_blocked"


@pytest.mark.asyncio
async def test_rule_telegram_to_ml_allows_ml_with_other_source(db) -> None:
    ctx = _FakeCtx(db=db)
    oid = _insert_outbox(
        db, payload={"marketplace": "mercadolibre", "source": "mercadolibre_hunter"}
    )
    assert (await rule_telegram_to_ml(ctx, {"outbox_id": oid})).skipped is False


# ---------------------------------------------------------------------------
# apply_hard_rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_hard_rules_returns_first_skip(db) -> None:
    ctx = _FakeCtx(db=db, scheduler=_FakeScheduler(mode=ScheduleMode.HIBERNATING))
    ctx.last_normal_publication_at = datetime.now(timezone.utc)  # también dispara cooldown
    # schedule_authority debe ganar (orden estable)
    out = await apply_hard_rules(
        ctx, ("schedule_authority", "cooldown_normal"), {}
    )
    assert out.skipped is True
    assert out.reason == "hibernating"


@pytest.mark.asyncio
async def test_apply_hard_rules_returns_ok_when_all_pass(db) -> None:
    ctx = _FakeCtx(db=db)
    out = await apply_hard_rules(ctx, ("schedule_authority", "cooldown_normal"), {})
    assert out.skipped is False


def test_list_rule_names_returns_seven() -> None:
    names = list_rule_names()
    assert set(names) == {
        "schedule_authority",
        "marketplace_paused",
        "cooldown_normal",
        "publishing_safe_mode",
        "image_price_url",
        "ml_affiliate",
        "telegram_to_ml",
    }
