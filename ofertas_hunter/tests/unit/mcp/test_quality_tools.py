"""Tests para las 4 quality tools del MCP server."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ofertas_hunter.config import Settings
from ofertas_hunter.db import connect, init_db
from ofertas_hunter.mcp.context import ServerContext
from ofertas_hunter.mcp.server import MCPServer
from ofertas_hunter.mcp.tools.quality_tools import build_quality_tools


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


@pytest.fixture
def server(ctx) -> MCPServer:
    registry = {spec.name: spec for spec in build_quality_tools(ctx)}
    return MCPServer(ctx, registry=registry)


_seed_n = [0]


def _seed_outbox(
    db: sqlite3.Connection,
    *,
    type_: str = "normal",
    payload: dict | None = None,
) -> int:
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
    _seed_n[0] += 1
    n = _seed_n[0]
    cur = db.execute(
        "INSERT INTO products (marketplace, marketplace_id, url_canonical, title, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("amazon", f"P{n}", f"https://x/p/{n}", "T", "2026-01-01", "2026-01-01"),
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
        (oid, type_, "2026-01-01T00:00:00Z", 0, "pending", json.dumps(payload)),
    )
    db.commit()
    return cur.lastrowid


def _payload(db, outbox_id: int) -> dict:
    row = db.execute(
        "SELECT message_payload_json, state FROM outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()
    return {
        "payload": json.loads(row["message_payload_json"]),
        "state": row["state"],
    }


# ---------------------------------------------------------------------------
# request_offer_review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_offer_review_creates_session_with_token(server, db) -> None:
    oid = _seed_outbox(db)
    out = await server.dispatch("request_offer_review", {"outbox_id": oid})
    assert "review_token" in out
    assert out["outbox"]["id"] == oid
    assert "formatted_preview" in out
    assert out["formatted_preview"]["text"]


@pytest.mark.asyncio
async def test_request_offer_review_skipped_when_image_missing(server, db) -> None:
    oid = _seed_outbox(
        db, payload={"current_price": 1.0, "url": "https://x"}
    )
    out = await server.dispatch("request_offer_review", {"outbox_id": oid})
    assert out["skipped"] is True and out["reason"] == "missing_image_url"


@pytest.mark.asyncio
async def test_request_offer_review_skipped_when_ml_missing_affiliate(db) -> None:
    """Este test requiere affiliate_required=True independientemente del .env."""
    from ofertas_hunter.config import Settings
    from ofertas_hunter.mcp.context import ServerContext
    from ofertas_hunter.mcp.server import MCPServer
    from ofertas_hunter.mcp.tools.quality_tools import build_quality_tools

    # Forzar affiliate_required=True para este test
    settings = Settings(mercadolibre_affiliate_required_for_publish=True)
    ctx_aff = ServerContext.build(db=db, settings=settings)
    registry = {spec.name: spec for spec in build_quality_tools(ctx_aff)}
    server_aff = MCPServer(ctx_aff, registry=registry)

    oid = _seed_outbox(
        db,
        payload={
            "marketplace": "mercadolibre",
            "image_url": "https://x",
            "current_price": 1.0,
            "url": "https://x",
        },
    )
    out = await server_aff.dispatch("request_offer_review", {"outbox_id": oid})
    assert out["skipped"] is True and out["reason"] == "missing_affiliate_url"


@pytest.mark.asyncio
async def test_request_offer_review_skipped_when_telegram_to_ml(server, db) -> None:
    oid = _seed_outbox(
        db,
        payload={
            "marketplace": "mercadolibre",
            "source": "telegram",
            "affiliate_url": "https://aff",
            "image_url": "https://x",
            "current_price": 1.0,
        },
    )
    out = await server.dispatch("request_offer_review", {"outbox_id": oid})
    assert out["skipped"] is True and out["reason"] == "telegram_to_ml_blocked"


# ---------------------------------------------------------------------------
# submit_offer_review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_offer_review_approve_marks_eligible(server, db) -> None:
    oid = _seed_outbox(db)
    req = await server.dispatch("request_offer_review", {"outbox_id": oid})
    out = await server.dispatch(
        "submit_offer_review",
        {"outbox_id": oid, "review_token": req["review_token"], "decision": "approve"},
    )
    assert out["success"] is True
    state = _payload(db, oid)["state"]
    assert state == "pending"


@pytest.mark.asyncio
async def test_submit_offer_review_reject_with_reason_marks_discarded(server, db) -> None:
    oid = _seed_outbox(db)
    req = await server.dispatch("request_offer_review", {"outbox_id": oid})
    out = await server.dispatch(
        "submit_offer_review",
        {
            "outbox_id": oid,
            "review_token": req["review_token"],
            "decision": "reject",
            "reason": "looks_fake",
        },
    )
    assert out["reason"] == "looks_fake"
    info = _payload(db, oid)
    assert info["state"] == "discarded"
    rows = db.execute(
        "SELECT reason FROM discarded_candidates WHERE source='mcp_review'"
    ).fetchall()
    assert any("looks_fake" in r["reason"] for r in rows)


@pytest.mark.asyncio
async def test_submit_offer_review_rewrite_message_updates_caption_only(server, db) -> None:
    oid = _seed_outbox(db)
    req = await server.dispatch("request_offer_review", {"outbox_id": oid})
    snapshot = _payload(db, oid)["payload"]

    out = await server.dispatch(
        "submit_offer_review",
        {
            "outbox_id": oid,
            "review_token": req["review_token"],
            "decision": "rewrite_message",
            "new_text": "🔥 Mejor copy escrito por Claude",
        },
    )
    assert out["success"] is True
    final = _payload(db, oid)["payload"]
    assert final["caption_override"] == "🔥 Mejor copy escrito por Claude"
    # Campos materiales intactos
    for key in (
        "image_url",
        "url",
        "current_price",
        "previous_price",
        "discount_percent",
        "marketplace",
        "title",
    ):
        assert final.get(key) == snapshot.get(key), f"campo {key} alterado"


@pytest.mark.asyncio
async def test_submit_offer_review_invalid_token_returns_error(server, db) -> None:
    oid = _seed_outbox(db)
    out = await server.dispatch(
        "submit_offer_review",
        {"outbox_id": oid, "review_token": "fake", "decision": "approve"},
    )
    assert out["error"] == "invalid_or_expired_token"


@pytest.mark.asyncio
async def test_submit_offer_review_token_mismatch_returns_error(server, db) -> None:
    oid_a = _seed_outbox(db)
    oid_b = _seed_outbox(db)
    req = await server.dispatch("request_offer_review", {"outbox_id": oid_a})
    # token de A usado contra B
    out = await server.dispatch(
        "submit_offer_review",
        {
            "outbox_id": oid_b,
            "review_token": req["review_token"],
            "decision": "approve",
        },
    )
    assert out["error"] == "invalid_or_expired_token"


@pytest.mark.asyncio
async def test_submit_offer_review_unknown_decision_validation_failed(server, db) -> None:
    oid = _seed_outbox(db)
    req = await server.dispatch("request_offer_review", {"outbox_id": oid})
    out = await server.dispatch(
        "submit_offer_review",
        {
            "outbox_id": oid,
            "review_token": req["review_token"],
            "decision": "obliterate",
        },
    )
    assert out["error"] == "validation_failed"


@pytest.mark.asyncio
async def test_submit_offer_review_missing_new_text_for_rewrite_returns_error(server, db) -> None:
    oid = _seed_outbox(db)
    req = await server.dispatch("request_offer_review", {"outbox_id": oid})
    out = await server.dispatch(
        "submit_offer_review",
        {
            "outbox_id": oid,
            "review_token": req["review_token"],
            "decision": "rewrite_message",
        },
    )
    assert out["error"] == "missing_new_text"


# ---------------------------------------------------------------------------
# improve_message_copy / submit_message_copy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_improve_message_copy_returns_token_and_payload(server, db) -> None:
    oid = _seed_outbox(db)
    out = await server.dispatch(
        "improve_message_copy", {"outbox_id": oid, "current_text": "old"}
    )
    assert "review_token" in out
    assert out["outbox"]["id"] == oid


@pytest.mark.asyncio
async def test_improve_message_copy_refuses_when_image_missing(server, db) -> None:
    oid = _seed_outbox(
        db, payload={"current_price": 1.0, "url": "https://x"}
    )
    out = await server.dispatch("improve_message_copy", {"outbox_id": oid})
    assert out["skipped"] is True and out["reason"] == "missing_image_url"


@pytest.mark.asyncio
async def test_submit_message_copy_updates_caption_only(server, db) -> None:
    oid = _seed_outbox(db)
    req = await server.dispatch("improve_message_copy", {"outbox_id": oid})
    snapshot = _payload(db, oid)["payload"]
    out = await server.dispatch(
        "submit_message_copy",
        {
            "outbox_id": oid,
            "review_token": req["review_token"],
            "new_text": "Nuevo texto pulido",
        },
    )
    assert out["success"] is True
    final = _payload(db, oid)["payload"]
    assert final["caption_override"] == "Nuevo texto pulido"
    for key in ("image_url", "url", "current_price", "previous_price", "marketplace"):
        assert final.get(key) == snapshot.get(key)


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


def test_build_quality_tools_returns_four_specs(ctx) -> None:
    specs = build_quality_tools(ctx)
    names = {s.name for s in specs}
    assert names == {
        "request_offer_review",
        "submit_offer_review",
        "improve_message_copy",
        "submit_message_copy",
    }


def test_quality_tools_have_additional_properties_false(ctx) -> None:
    for spec in build_quality_tools(ctx):
        d = spec.descriptor()
        assert d.inputSchema.get("additionalProperties") is False, spec.name
