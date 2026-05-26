"""Tests del comando `audit-false-price-errors`.

Cubre:
- detección de items outbox `price_error` cuyo título es accesorio genérico
- emisión de runtime_event `false_price_error_detected`
- modo `--fix`: discard y degrade-to-normal
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ofertas_hunter.db import connect, init_db
from ofertas_hunter.intelligence.false_price_error_audit import run_audit
from ofertas_hunter.models import OutboxState, OutboxType


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _enqueue_outbox(
    conn,
    *,
    outbox_id: int,
    payload: dict,
    type_: str = OutboxType.PRICE_ERROR.value,
    state: str = OutboxState.PENDING.value,
) -> None:
    # Insertamos un product mínimo + offer + outbox.
    conn.execute(
        "INSERT OR IGNORE INTO products (id, marketplace, url_canonical, title, condition, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, 'new', ?, ?)",
        (
            outbox_id,
            payload.get("marketplace") or "mercadolibre",
            payload["url"],
            payload["title"],
            _now_iso(),
            _now_iso(),
        ),
    )
    conn.execute(
        "INSERT INTO offers (id, product_id, classification, score, reasons_json, "
        "discount_percent, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            outbox_id,
            outbox_id,
            "possible_price_error",
            65,
            "[]",
            payload.get("discount_percent"),
            "eligible",
            _now_iso(),
            _now_iso(),
        ),
    )
    conn.execute(
        "INSERT INTO outbox (id, offer_id, type, enqueued_at, attempts, state, "
        "message_payload_json) VALUES (?, ?, ?, ?, 0, ?, ?)",
        (
            outbox_id,
            outbox_id,
            type_,
            _now_iso(),
            state,
            json.dumps(payload, ensure_ascii=False),
        ),
    )


def test_audit_detects_false_pe_charger_without_fix(tmp_path: Path):
    db_path = tmp_path / "audit_no_fix.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        _enqueue_outbox(
            conn,
            outbox_id=101,
            payload={
                "title": "Cargador 20w Compatible con iPhone16/15/14/13/12",
                "current_price": 93.98,
                "url": "https://articulo.mercadolibre.com.mx/MLM-101",
                "image_url": "https://x/img.jpg",
                "confidence_label": "medium",
                "marketplace": "mercadolibre",
            },
        )
        report = run_audit(conn, fix=False, days=30)
    finally:
        conn.close()

    assert report.scanned == 1
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.outbox_id == 101
    assert finding.is_generic_accessory is True
    assert finding.mentions_compatible_with_premium is True
    assert finding.action == "logged_only"  # sin --fix no muta nada


def test_audit_fix_discards_when_no_real_discount(tmp_path: Path):
    db_path = tmp_path / "audit_discard.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        _enqueue_outbox(
            conn,
            outbox_id=201,
            payload={
                "title": "Cargador TIPO C GaN Turbo 20W Compatible con iPhone",
                "current_price": 76.62,
                "url": "https://articulo.mercadolibre.com.mx/MLM-201",
                "image_url": "https://x/img.jpg",
                "confidence_label": "medium",
                "marketplace": "mercadolibre",
            },
        )
        report = run_audit(conn, fix=True, days=30)
        conn.commit()

        # State del outbox después del audit
        row = conn.execute(
            "SELECT state, message_payload_json FROM outbox WHERE id=?",
            (201,),
        ).fetchone()
        # Runtime events emitidos
        ev_count = conn.execute(
            "SELECT COUNT(*) AS n FROM runtime_events "
            "WHERE kind='false_price_error_detected'"
        ).fetchone()["n"]
    finally:
        conn.close()

    assert report.discarded == 1
    assert row["state"] == OutboxState.DISCARDED.value
    payload = json.loads(row["message_payload_json"])
    assert "discarded_reason" in payload
    assert ev_count == 1


def test_audit_fix_degrades_to_normal_when_discount_50(tmp_path: Path):
    db_path = tmp_path / "audit_degrade.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        _enqueue_outbox(
            conn,
            outbox_id=301,
            payload={
                "title": "Cargador Rápida 20w + 2 Metros Cable Tipo C Para iPhone",
                "current_price": 160.05,
                "previous_price": 400.0,
                "discount_percent": 60.0,
                "url": "https://articulo.mercadolibre.com.mx/MLM-301",
                "image_url": "https://x/img.jpg",
                "confidence_label": "medium",
                "marketplace": "mercadolibre",
            },
        )
        report = run_audit(conn, fix=True, days=30)
        conn.commit()

        row = conn.execute(
            "SELECT type, message_payload_json FROM outbox WHERE id=?",
            (301,),
        ).fetchone()
    finally:
        conn.close()

    assert report.degraded == 1
    assert row["type"] == OutboxType.NORMAL.value
    payload = json.loads(row["message_payload_json"])
    assert payload.get("degraded_from") == OutboxType.PRICE_ERROR.value
    assert payload.get("discount_percent") == 60.0


def test_audit_skips_published_real_premium_pe(tmp_path: Path):
    """Un PE real (iPhone 16 Pro Max) NO debe marcarse como falso positivo."""
    db_path = tmp_path / "audit_real.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        _enqueue_outbox(
            conn,
            outbox_id=401,
            payload={
                "title": "Apple iPhone 16 Pro Max 256GB Titanio Natural",
                "current_price": 3899.0,
                "url": "https://liverpool.com.mx/p/iphone16",
                "image_url": "https://x/img.jpg",
                "confidence_label": "very high",
                "marketplace": "liverpool",
            },
        )
        report = run_audit(conn, fix=True, days=30)
    finally:
        conn.close()

    assert report.scanned == 1
    assert len(report.findings) == 0  # no detectado como falso positivo
