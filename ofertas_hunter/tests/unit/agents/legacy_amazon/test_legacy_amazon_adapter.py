"""Tests del adapter `legacy_amazon.adapter` (criterios A, C, D, E, F).

Cubre:
- F1: usa el `PriceErrorScorer` nuevo.
- F2: NO usa el verdict legacy como fuente de verdad.
- F3: requiere imagen.
- F4: requiere precio actual.
- F5: encola normal_offer cuando descuento >= 50%.
- F6: encola price_error solo con confianza alta.
- C: distingue captcha real vs DOM incompleto (no pausa por DOM
  incompleto).
- E: registra razón de descarte en `discarded_candidates` /
  `runtime_events`.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from ofertas_hunter.agents.legacy_amazon.adapter import (
    AdapterOutcome,
    process_legacy_fetch_result,
)
from ofertas_hunter.agents.legacy_amazon.worker import LegacyFetchResult
from ofertas_hunter.db import init_db
from ofertas_hunter.intelligence.price_error_scorer import PriceErrorScorer
from ofertas_hunter.models import Classification


# ---------------------------------------------------------------------------
# Fixtures de DB en memoria
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    """Inicializa una DB SQLite efímera con el schema completo del bot."""
    from pathlib import Path

    path = tmp_path / "test.db"
    init_db(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def scorer():
    return PriceErrorScorer()


# ---------------------------------------------------------------------------
# Helpers para construir LegacyFetchResult sintéticos
# ---------------------------------------------------------------------------


def _ok_fetch(data: dict) -> LegacyFetchResult:
    # Simula el comportamiento del extractor real: cuando hay un precio
    # anterior válido (mayor que el actual), marca la verificación. Los tests
    # que quieran simular un precio anterior NO verificado pueden pasar
    # explícitamente `old_price_verified=False` en el `data`.
    data = dict(data)
    has_valid_prev = (
        data.get("price_original") is not None
        and data.get("price_current") is not None
        and data["price_original"] > data["price_current"]
    )
    data.setdefault("old_price_verified", has_valid_prev)
    if data.get("old_price_verified"):
        data.setdefault("old_price_source", "basis_price_strike")
        data.setdefault("discount_source", "calculated_verified")
        data.setdefault(
            "discount_percent_verified", data.get("discount_percent") is not None
        )
    else:
        data.setdefault("discount_percent_verified", False)
    return LegacyFetchResult(
        ok=True,
        final_url=data.get("url", "https://www.amazon.com.mx/dp/B0TEST00001"),
        status=200,
        captcha=False,
        captcha_confidence=None,
        data=data,
        reason=None,
    )


def _captcha_fetch(*, confidence: str, signals=("form_action_validate_captcha",)) -> LegacyFetchResult:
    return LegacyFetchResult(
        ok=False,
        final_url="https://www.amazon.com.mx/errors/validateCaptcha",
        status=200,
        captcha=True,
        captcha_confidence=confidence,
        data=None,
        reason="captcha",
        captcha_signals=tuple(signals),
    )


def _dom_incomplete_fetch(data=None) -> LegacyFetchResult:
    return LegacyFetchResult(
        ok=False,
        final_url="https://www.amazon.com.mx/dp/B0TEST00001",
        status=200,
        captcha=False,
        captcha_confidence=None,
        data=data,
        reason="dom_incomplete",
    )


# ---------------------------------------------------------------------------
# F1, F2: usa PriceErrorScorer y NO el verdict legacy
# ---------------------------------------------------------------------------


def test_legacy_amazon_adapter_uses_new_price_error_scorer(db, scorer):
    """El scoring final viene del PriceErrorScorer nuevo, no de heurísticas
    legacy. Verificable: el item en `outbox.message_payload_json` lleva
    `score_classification` con valores del enum nuevo
    (`no_price_error`, `possible_price_error`, ...) y `score` numérico.
    """
    data = {
        "url": "https://www.amazon.com.mx/dp/B0NORMAL01",
        "title": "Lavadora Mabe 17 kg",
        "price_current": 4999.0,
        "price_original": 9999.0,
        "discount_percent": 50.0,
        "image_url": "https://m.media-amazon.com/images/I/example1.jpg",
        "asin": "B0NORMAL01",
        "availability": "En stock",
    }
    outcome = process_legacy_fetch_result(
        db, scorer, _ok_fetch(data), original_url=data["url"]
    )
    assert outcome.enqueued_outbox_id is not None
    # En outbox queda el classification del enum nuevo:
    row = db.execute(
        "SELECT message_payload_json FROM outbox WHERE id=?",
        (outcome.enqueued_outbox_id,),
    ).fetchone()
    payload = json.loads(row["message_payload_json"])
    valid_values = {c.value for c in Classification}
    assert payload["score_classification"] in valid_values
    assert isinstance(payload["score"], int)
    assert payload.get("brand") is None or isinstance(payload.get("brand"), str)
    assert payload.get("category") is None or isinstance(payload.get("category"), str)
    # `extractor` lo identifica como legacy:
    assert payload.get("extractor") == "legacy_amazon"


def test_legacy_amazon_adapter_does_not_use_legacy_verdict_as_final_truth(db, scorer):
    """Aunque el dict legacy traiga campos como `ai_evaluation` o
    `verdict='EXCELENTE'`, el adapter ignora esos y aplica solo
    `PriceErrorScorer`. El payload outbox no debe contener verdict legacy.
    """
    data = {
        "url": "https://www.amazon.com.mx/dp/B0NOAIVERD",
        "title": "Audífonos Bluetooth genérico chino",
        "price_current": 80.0,
        "price_original": 800.0,
        "discount_percent": 90.0,
        "image_url": "https://m.media-amazon.com/images/I/x.jpg",
        "asin": "B0NOAIVERD",
        # Estos NUNCA deben influir:
        "ai_evaluation": {"verdict": "EXCELENTE", "score": 95, "genuine_discount": True},
    }
    outcome = process_legacy_fetch_result(
        db, scorer, _ok_fetch(data), original_url=data["url"]
    )
    if outcome.enqueued_outbox_id is not None:
        row = db.execute(
            "SELECT message_payload_json FROM outbox WHERE id=?",
            (outcome.enqueued_outbox_id,),
        ).fetchone()
        payload = json.loads(row["message_payload_json"])
        assert "verdict" not in payload
        assert "ai_evaluation" not in payload
    # Como es accesorio audio, el scorer lo capa: no debe ser
    # price_error_confirmed.
    assert outcome.classification != Classification.PRICE_ERROR_CONFIRMED.value


# ---------------------------------------------------------------------------
# F3, F4: gates duros
# ---------------------------------------------------------------------------


def test_legacy_amazon_adapter_requires_image(db, scorer):
    data = {
        "url": "https://www.amazon.com.mx/dp/B0NOIMG0001",
        "title": "Lavadora Mabe sin imagen",
        "price_current": 4999.0,
        "price_original": 9999.0,
        "discount_percent": 50.0,
        "image_url": None,  # FALTA
        "asin": "B0NOIMG0001",
    }
    outcome = process_legacy_fetch_result(
        db, scorer, _ok_fetch(data), original_url=data["url"]
    )
    assert outcome.enqueued_outbox_id is None
    assert outcome.discarded_reason is not None
    assert "image_url" in outcome.discarded_reason
    # Discard se persistió:
    discards = db.execute(
        "SELECT reason FROM discarded_candidates WHERE source='amazon_hunter'"
    ).fetchall()
    assert any("image_url" in r["reason"] for r in discards)


def test_legacy_amazon_adapter_requires_current_price(db, scorer):
    data = {
        "url": "https://www.amazon.com.mx/dp/B0NOPRICE01",
        "title": "Producto sin precio actual",
        "price_current": None,  # FALTA
        "price_original": 9999.0,
        "discount_percent": 50.0,
        "image_url": "https://m.media-amazon.com/images/I/x.jpg",
        "asin": "B0NOPRICE01",
    }
    outcome = process_legacy_fetch_result(
        db, scorer, _ok_fetch(data), original_url=data["url"]
    )
    assert outcome.enqueued_outbox_id is None
    assert outcome.discarded_reason is not None
    assert "current_price" in outcome.discarded_reason


def test_legacy_amazon_adapter_requires_url(db, scorer):
    data = {
        "url": "",  # FALTA url
        "title": "Producto sin URL",
        "price_current": 100.0,
        "discount_percent": 50.0,
        "image_url": "https://m.media-amazon.com/images/I/x.jpg",
    }
    outcome = process_legacy_fetch_result(
        db, scorer, _ok_fetch(data), original_url=""
    )
    assert outcome.enqueued_outbox_id is None
    assert outcome.discarded_reason is not None


# ---------------------------------------------------------------------------
# F5: encola normal_offer >= 50%
# ---------------------------------------------------------------------------


def test_legacy_amazon_adapter_enqueues_normal_offer_over_50(db, scorer):
    data = {
        "url": "https://www.amazon.com.mx/dp/B0NRM50PCT",
        "title": "Lavadora Mabe Carga Superior 17 kg Blanco",
        "price_current": 4999.0,
        "price_original": 9999.0,
        "discount_percent": 50.0,
        "image_url": "https://m.media-amazon.com/images/I/normal.jpg",
        "asin": "B0NRM50PCT",
        "availability": "En stock",
    }
    outcome = process_legacy_fetch_result(
        db, scorer, _ok_fetch(data), original_url=data["url"]
    )
    assert outcome.enqueued_outbox_id is not None
    row = db.execute(
        "SELECT type FROM outbox WHERE id=?", (outcome.enqueued_outbox_id,)
    ).fetchone()
    assert row["type"] == "normal"


def test_legacy_amazon_adapter_does_not_enqueue_normal_below_50(db, scorer):
    data = {
        "url": "https://www.amazon.com.mx/dp/B0NRM30PCT",
        "title": "Lavadora con 30% off (insuficiente)",
        "price_current": 7000.0,
        "price_original": 10000.0,
        "discount_percent": 30.0,
        "image_url": "https://m.media-amazon.com/images/I/x.jpg",
        "asin": "B0NRM30PCT",
    }
    outcome = process_legacy_fetch_result(
        db, scorer, _ok_fetch(data), original_url=data["url"]
    )
    assert outcome.enqueued_outbox_id is None
    assert outcome.discarded_reason == "no_outbox_type_below_thresholds"


# ---------------------------------------------------------------------------
# F6: price_error solo con high confidence
# ---------------------------------------------------------------------------


def test_legacy_amazon_adapter_enqueues_price_error_only_high_confidence(db, scorer):
    """Un iPhone 15 Pro Max a $3,899 (90% off) debe ser
    `price_error_confirmed` (high). Lo encola como `price_error`.
    """
    data = {
        "url": "https://www.amazon.com.mx/dp/B0IPHONE15",
        "title": "Apple iPhone 15 Pro Max 256GB Titanio Natural",
        "price_current": 3899.0,
        "price_original": 39999.0,
        "discount_percent": 90.0,
        "image_url": "https://m.media-amazon.com/images/I/iphone.jpg",
        "asin": "B0IPHONE15",
        "availability": "En stock",
    }
    outcome = process_legacy_fetch_result(
        db, scorer, _ok_fetch(data), original_url=data["url"]
    )
    assert outcome.enqueued_outbox_id is not None
    row = db.execute(
        "SELECT type FROM outbox WHERE id=?", (outcome.enqueued_outbox_id,)
    ).fetchone()
    assert row["type"] == "price_error"
    assert outcome.classification == Classification.PRICE_ERROR_CONFIRMED.value


def test_legacy_amazon_adapter_does_not_promote_audio_to_price_error(db, scorer):
    """Regresión del fix anterior (Redmi Buds): un accesorio de audio,
    aun con descuento alto y previous_price, NO debe ser
    `price_error_confirmed`. El cap de accesorio (55) lo contiene.
    """
    data = {
        "url": "https://www.amazon.com.mx/dp/B0REDMIBUD",
        "title": "XIAOMI Audífonos Redmi Buds 6 Play Negro",
        "price_current": 199.0,
        "price_original": 1429.0,
        "discount_percent": 86.0,
        "image_url": "https://m.media-amazon.com/images/I/buds.jpg",
        "asin": "B0REDMIBUD",
    }
    outcome = process_legacy_fetch_result(
        db, scorer, _ok_fetch(data), original_url=data["url"]
    )
    assert outcome.classification != Classification.PRICE_ERROR_CONFIRMED.value


# ---------------------------------------------------------------------------
# Criterio C: captcha real vs DOM incompleto
# ---------------------------------------------------------------------------


def test_legacy_amazon_does_not_pause_on_missing_price_as_captcha(db, scorer):
    """Una página donde el extractor falló pero NO hay señales de captcha
    NO debe ser tratada como captcha. El adapter registra
    `discarded_candidates` con razón `dom_incomplete` y NO emite el
    evento `amazon_legacy_captcha_confirmed`.
    """
    outcome = process_legacy_fetch_result(
        db,
        scorer,
        _dom_incomplete_fetch(data={"title": None, "price_current": None}),
        original_url="https://www.amazon.com.mx/dp/B0DOMFAIL01",
    )
    assert outcome.captcha is False
    assert outcome.captcha_confidence is None
    assert outcome.discarded_reason == "dom_incomplete"
    # No debe haber registrado captcha en runtime_events:
    captcha_events = db.execute(
        "SELECT 1 FROM runtime_events WHERE kind='amazon_legacy_captcha_confirmed'"
    ).fetchall()
    assert len(captcha_events) == 0
    # Sí debe estar en discarded_candidates:
    discards = db.execute(
        "SELECT reason FROM discarded_candidates WHERE reason='dom_incomplete'"
    ).fetchall()
    assert len(discards) == 1


def test_legacy_amazon_pauses_on_real_captcha_high_confidence(db, scorer):
    outcome = process_legacy_fetch_result(
        db,
        scorer,
        _captcha_fetch(confidence="high"),
        original_url="https://www.amazon.com.mx/dp/B0CAPTCHA1",
    )
    assert outcome.captcha is True
    assert outcome.captcha_confidence == "high"
    # Emitió el evento:
    events = db.execute(
        "SELECT severity FROM runtime_events WHERE kind='amazon_legacy_captcha_confirmed'"
    ).fetchall()
    assert len(events) == 1
    assert events[0]["severity"] == "error"


def test_legacy_amazon_does_not_pause_on_medium_confidence_captcha(db, scorer):
    """Captcha medium = una sola señal estructural, podría ser falso
    positivo. El adapter registra como sospecha pero NO marca
    should_pause_marketplace ni emite el evento high.
    """
    outcome = process_legacy_fetch_result(
        db,
        scorer,
        _captcha_fetch(confidence="medium"),
        original_url="https://www.amazon.com.mx/dp/B0CAPMED01",
    )
    assert outcome.captcha is True
    assert outcome.captcha_confidence == "medium"
    # NO debe haber generado el evento high-confidence:
    events_high = db.execute(
        "SELECT 1 FROM runtime_events WHERE kind='amazon_legacy_captcha_confirmed'"
    ).fetchall()
    assert len(events_high) == 0
    # Sí registra sospecha:
    events_suspect = db.execute(
        "SELECT 1 FROM runtime_events WHERE kind='amazon_legacy_captcha_suspect'"
    ).fetchall()
    assert len(events_suspect) == 1


# ---------------------------------------------------------------------------
# Criterio E: registro de razones
# ---------------------------------------------------------------------------


def test_legacy_amazon_adapter_records_discard_reason_in_runtime_events(db, scorer):
    """Cuando descarta por captcha, el evento queda en runtime_events
    con detalle (url, signals, confidence)."""
    outcome = process_legacy_fetch_result(
        db,
        scorer,
        _captcha_fetch(
            confidence="high",
            signals=("form_action_validate_captcha", "input_captchacharacters"),
        ),
        original_url="https://www.amazon.com.mx/dp/B0AUDREASON",
    )
    row = db.execute(
        "SELECT payload_json FROM runtime_events WHERE kind='amazon_legacy_captcha_confirmed'"
    ).fetchone()
    assert row is not None
    payload = json.loads(row["payload_json"])
    assert payload["url"] == "https://www.amazon.com.mx/dp/B0AUDREASON"
    assert "form_action_validate_captcha" in payload["signals"]
    assert payload["confidence"] == "high"


# ---------------------------------------------------------------------------
# Idempotencia: mismo ASIN no se re-encola si ya fue publicado <48h
# ---------------------------------------------------------------------------


def test_legacy_amazon_adapter_skips_recently_published_asin(db, scorer):
    """Si el ASIN ya tiene un outbox state='sent' reciente, el adapter
    no encola otra vez."""
    # Insert un product/offer/outbox sent ya en DB:
    db.execute(
        "INSERT INTO products (marketplace, marketplace_id, url_canonical, title, "
        "condition, first_seen_at, last_seen_at) "
        "VALUES ('amazon', 'B0RECENT001', 'https://www.amazon.com.mx/dp/B0RECENT001', "
        "'Lavadora ya publicada', 'new', datetime('now','-1 hour'), datetime('now','-1 hour'))"
    )
    pid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        "INSERT INTO offers (product_id, classification, score, reasons_json, state, "
        "created_at, updated_at) VALUES (?, 'no_price_error', 0, '[]', 'eligible', "
        "datetime('now'), datetime('now'))",
        (pid,),
    )
    oid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        "INSERT INTO outbox (offer_id, type, enqueued_at, attempts, state, "
        "last_attempt_at, message_payload_json) "
        "VALUES (?, 'normal', datetime('now','-1 hour'), 1, 'sent', "
        "datetime('now','-1 hour'), '{}')",
        (oid,),
    )
    db.commit()

    # Intentamos encolar el mismo ASIN otra vez:
    data = {
        "url": "https://www.amazon.com.mx/dp/B0RECENT001",
        "title": "Lavadora Mabe ya publicada",
        "price_current": 4999.0,
        "price_original": 9999.0,
        "discount_percent": 50.0,
        "image_url": "https://m.media-amazon.com/images/I/x.jpg",
        "asin": "B0RECENT001",
    }
    outcome = process_legacy_fetch_result(
        db, scorer, _ok_fetch(data), original_url=data["url"]
    )
    assert outcome.enqueued_outbox_id is None
    assert outcome.discarded_reason == "recently_published"
