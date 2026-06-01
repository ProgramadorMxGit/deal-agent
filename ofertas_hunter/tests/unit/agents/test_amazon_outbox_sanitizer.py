"""Tests del clasificador de saneamiento de outbox Amazon."""

from __future__ import annotations

from ofertas_hunter.agents.amazon_outbox_sanitizer import classify_amazon_pending


def _payload(**over):
    base = {
        "marketplace": "amazon",
        "type": "normal",
        "current_price": 350,
        "previous_price": 800,
        "discount_percent": 56,
        "old_price_verified": True,
        "current_price_verified": True,
        "current_price_raw_text": "$350.00",
        "affiliate_url": "https://amzn.to/4abc",
    }
    base.update(over)
    return base


def test_ok_when_affiliate_and_verified_old_price():
    assert classify_amazon_pending(_payload(), item_type="normal") == "ok"


def test_needs_affiliate_when_missing():
    assert (
        classify_amazon_pending(_payload(affiliate_url=None), item_type="normal")
        == "amazon_missing_affiliate"
    )


def test_needs_affiliate_when_invalid_link():
    assert (
        classify_amazon_pending(
            _payload(affiliate_url="https://www.amazon.com.mx/dp/B0X"),
            item_type="normal",
        )
        == "amazon_missing_affiliate"
    )


def test_no_verified_old_price_blocks_normal():
    assert (
        classify_amazon_pending(
            _payload(old_price_verified=False), item_type="normal"
        )
        == "amazon_no_verified_old_price"
    )


def test_unit_pack_keyword_title_is_suspicious():
    """Título de guantes/pack con precio que parece unitario → suspicious."""
    p = _payload(
        title="Guantes de nitrilo desechables caja 100 piezas",
        current_price=8.5,
        previous_price=189.0,
        current_price_raw_text="$8.50",
    )
    # 95% → extreme primero, pero igual debe bloquear (no publicable).
    assert classify_amazon_pending(p, item_type="normal") in (
        "amazon_extreme_discount_unverified",
        "amazon_current_price_suspicious",
    )


def test_unit_pack_keyword_with_moderate_discount_is_suspicious():
    """Keywords de pack + precio bajo absoluto aunque el descuento no sea extremo."""
    p = _payload(
        title="Pack guantes latex 50 unidades",
        current_price=9.0,
        previous_price=45.0,  # 80% < 90, cur<10 pero prev<50 → no entra 2c
        current_price_raw_text="$9.00",
    )
    assert (
        classify_amazon_pending(p, item_type="normal")
        == "amazon_current_price_suspicious"
    )


def test_price_error_does_not_require_old_price():
    """Un price_error con afiliado válido pasa aunque no tenga old price."""
    p = _payload(old_price_verified=False, discount_percent=None, previous_price=None)
    assert classify_amazon_pending(p, item_type="price_error") == "ok"


def test_non_amazon_returns_skip():
    assert (
        classify_amazon_pending(
            {"marketplace": "mercadolibre", "affiliate_url": "https://meli.la/x"},
            item_type="normal",
        )
        == "skip_non_amazon"
    )


def test_unit_price_raw_text_is_suspicious():
    p = _payload(current_price=0.69, previous_price=119.0,
                 current_price_raw_text="$0.69 / unidad")
    assert classify_amazon_pending(p, item_type="normal") == "amazon_unit_price_as_current_price"


def test_extreme_discount_is_suspicious_without_verified():
    p = _payload(current_price=0.6, previous_price=332.0)
    assert classify_amazon_pending(p, item_type="normal") == "amazon_extreme_discount_unverified"


def test_low_absolute_price_is_suspicious():
    # 94% < 90? no, >=90 → extreme. Usa un caso <10 con disc <90.
    p = _payload(current_price=7.0, previous_price=80.0)  # 91% → extreme
    assert classify_amazon_pending(p, item_type="normal") in (
        "amazon_extreme_discount_unverified", "amazon_current_price_suspicious",
    )


def test_extreme_discount_ok_when_verified():
    p = _payload(current_price=30.0, previous_price=3000.0,
                 extreme_discount_verified=True, current_price_raw_text="$30.00",
                 current_price_verified=True)
    assert classify_amazon_pending(p, item_type="normal") == "ok"


def test_missing_affiliate_reason():
    p = _payload(affiliate_url=None)
    assert classify_amazon_pending(p, item_type="normal") == "amazon_missing_affiliate"


def test_no_verified_old_price_reason():
    p = _payload(old_price_verified=False)
    assert classify_amazon_pending(p, item_type="normal") == "amazon_no_verified_old_price"


def test_current_price_unverified_reason():
    """Old price verificado pero current_price no verificado → bloquea."""
    p = _payload(current_price=155.0, previous_price=349.0, discount_percent=56,
                 old_price_verified=True, current_price_verified=False)
    assert classify_amazon_pending(p, item_type="normal") == "amazon_current_price_unverified"


def test_telegram_amazon_needs_pdp_revalidation():
    p = _payload(source="telegram", affiliate_url="https://amzn.to/x",
                 old_price_verified=False, current_price_verified=False)
    assert classify_amazon_pending(p, item_type="normal") == "amazon_telegram_needs_pdp_revalidation"


def test_ok_when_all_verified():
    p = _payload(current_price=155.0, previous_price=349.0, discount_percent=56,
                 old_price_verified=True, current_price_verified=True,
                 current_price_raw_text="$155.00")
    assert classify_amazon_pending(p, item_type="normal") == "ok"


# ---------------------------------------------------------------------------
# Runner DB-level
# ---------------------------------------------------------------------------

import json
from pathlib import Path

import pytest

from ofertas_hunter.agents.amazon_outbox_sanitizer import sanitize_amazon_outbox
from ofertas_hunter.db import connect, init_db
from ofertas_hunter.models import OutboxState


_SEQ = [0]


def _enqueue(conn, payload, *, type_="normal"):
    _SEQ[0] += 1
    pcur = conn.execute(
        "INSERT INTO products (marketplace, marketplace_id, url_canonical, title, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("amazon", None, f"https://x/{_SEQ[0]}", "t",
         "2026-05-30T00:00:00Z", "2026-05-30T00:00:00Z"),
    )
    product_id = pcur.lastrowid
    ocur = conn.execute(
        "INSERT INTO offers (product_id, classification, score, reasons_json, "
        "discount_percent, state, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (product_id, "no_price_error", 0, "[]", None, "eligible",
         "2026-05-30T00:00:00Z", "2026-05-30T00:00:00Z"),
    )
    offer_id = ocur.lastrowid
    cur = conn.execute(
        "INSERT INTO outbox (offer_id, type, enqueued_at, scheduled_for, attempts, "
        "last_attempt_at, state, message_payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (offer_id, type_, "2026-05-30T00:00:00Z", None, 0, None,
         OutboxState.PENDING.value, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def conn(tmp_path: Path):
    init_db(tmp_path / "x.db")
    c = connect(tmp_path / "x.db")
    yield c
    c.close()


@pytest.mark.asyncio
async def test_sanitize_blocks_missing_affiliate_and_unverified(conn):
    ok_id = _enqueue(conn, _payload())  # ok
    no_aff = _enqueue(conn, _payload(affiliate_url=None))
    no_old = _enqueue(conn, _payload(old_price_verified=False))
    ml_id = _enqueue(conn, {"marketplace": "mercadolibre", "affiliate_url": "https://meli.la/x"})

    report = await sanitize_amazon_outbox(conn, enricher=None)

    assert report.blocked_affiliate == 1
    assert report.blocked_old_price == 1
    assert report.already_ok == 1

    def state(i):
        return conn.execute("SELECT state FROM outbox WHERE id=?", (i,)).fetchone()[0]

    assert state(ok_id) == OutboxState.PENDING.value
    assert state(no_aff) == OutboxState.DISCARDED.value
    assert state(no_old) == OutboxState.DISCARDED.value
    # ML no se toca.
    assert state(ml_id) == OutboxState.PENDING.value
