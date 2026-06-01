"""Tests del candidate builder (clasificación interna + signal/scoring)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ofertas_hunter.telegram.candidate_builder import (
    LISTENER_DEAL,
    LISTENER_IGNORED_ML,
    LISTENER_NOISE,
    LISTENER_PRICE_ERROR,
    TelegramCandidateBuilder,
)
from ofertas_hunter.telegram.link_resolver import ResolvedLink
from ofertas_hunter.telegram.message_parser import parse_message


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "telegram"


def _now() -> datetime:
    return datetime(2026, 5, 25, 14, 0, 0, tzinfo=timezone.utc)


def _parse_fixture(name: str, *, image_path: str | None = "/fake/img.jpg"):
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return parse_message(
        text,
        channel="ofertonesmexico",
        message_id=1,
        captured_at=_now(),
        image_path=image_path,
        chat_id=-100123,
    )


@pytest.mark.parametrize(
    "fixture",
    [
        "iphone_16_pro_max_liverpool_3899.txt",
        "asus_vivobook_sams_305.txt",
        "galaxy_s24_sears_1399.txt",
        "galaxy_a32_walmart_197.txt",
        "msi_coppel_2719.txt",
        "dell_pro_16_1544.txt",
        "ipad_coppel_2611.txt",
        "airpods_officedepot_599.txt",
        "laptop_hp_elitebook_walmart_2349.txt",
        "sony_wf1000xm5.txt",
    ],
)
def test_telegram_price_error_signal_gets_high_confidence(fixture):
    parsed = _parse_fixture(fixture)
    builder = TelegramCandidateBuilder()
    candidate = builder.build(parsed)

    assert candidate.internal_classification == LISTENER_PRICE_ERROR, (
        f"{fixture} → {candidate.internal_classification} reasons={candidate.reasons}"
    )
    assert candidate.scoring is not None
    assert candidate.scoring.confidence_label in ("high", "very high"), (
        f"{fixture} confidence={candidate.scoring.confidence_label} score={candidate.scoring.score}"
    )
    # Si tiene precio, link e imagen, debe haber outbox_item listo para
    # encolarse como pending_revalidation.
    assert candidate.outbox_item is not None
    assert candidate.outbox_item.message_payload["requires_live_validation"] is True


def test_telegram_does_not_create_candidate_for_noise():
    parsed = parse_message(
        "Mensaje cualquiera sin link ni precio",
        channel="x",
        message_id=99,
        captured_at=_now(),
    )
    candidate = TelegramCandidateBuilder().build(parsed)
    assert candidate.internal_classification == LISTENER_NOISE
    assert candidate.outbox_item is None


def test_telegram_ignores_mercadolibre_links():
    parsed = _parse_fixture("mercadolibre_link_should_be_ignored.txt")
    candidate = TelegramCandidateBuilder().build(parsed)
    assert candidate.internal_classification == LISTENER_IGNORED_ML
    assert candidate.outbox_item is None


def test_telegram_ignores_shortlink_resolved_to_mercadolibre():
    parsed = parse_message(
        "Producto random\nhttps://bit.ly/abc",
        channel="x",
        message_id=2,
        captured_at=_now(),
    )
    resolved = ResolvedLink(
        original_url="https://bit.ly/abc",
        final_url="https://www.mercadolibre.com.mx/p/MLM12345",
        http_status=200,
        resolved=True,
        is_mercadolibre=True,
    )
    candidate = TelegramCandidateBuilder().build(parsed, resolved=resolved)
    assert candidate.internal_classification == LISTENER_IGNORED_ML


def test_telegram_91_percent_discount_creates_actionable_candidate():
    parsed = _parse_fixture("coofandy_amazon_91_percent.txt")
    candidate = TelegramCandidateBuilder().build(parsed)
    # 91% off es señal extrema → el listener fuerza price_error_signal
    # incluso si el scorer marca suspicious_deal por falta de marca premium.
    assert candidate.internal_classification == LISTENER_PRICE_ERROR
    assert candidate.scoring is not None
    # Score puede caer en suspicious_deal (40-59) si no hay marca premium ni
    # categoría premium. Lo importante es que sea actionable.
    assert candidate.scoring.score >= 40
    assert candidate.outbox_item is not None


def test_telegram_no_image_no_outbox_item_safe():
    """Sin imagen, el builder no debería bloquear el flujo, pero sí marcar."""
    parsed = _parse_fixture("dell_pro_16_1544.txt", image_path=None)
    candidate = TelegramCandidateBuilder().build(parsed)
    # Tiene urgencia + precio → sigue siendo PE.
    assert candidate.internal_classification == LISTENER_PRICE_ERROR
    # Pero el scoring marca no_image como no publicable.
    assert candidate.scoring is not None
    assert candidate.scoring.is_publishable is False
    assert "no_image" in candidate.scoring.not_publishable_reasons


def test_amazon_coupon_format_without_visible_50_still_enqueues_for_live_validation():
    text = (
        "Amazon: Samsung Galaxy S25 Ultra Azul 256GB con S-Pen\n"
        "👉Ver Oferta:\n"
        "https://www.amazon.com.mx/dp/B0DNTX93TX\n"
        "✅Cupón de 30% pagando de contado o a Meses sin intereses con Tarjeta de crédito BANAMEX:\n"
        "BNMXHOT30\n"
        "➡️Compra mínima $12,500, ▶️Tope de descuento $5,000\n"
        "🔥Precio Oferta + ✅Cupón 30% \"BNMXHOT30\": $11,549"
    )
    parsed = parse_message(
        text,
        channel="ofertonesmexico",
        message_id=2,
        captured_at=_now(),
        image_path="/fake/img.jpg",
        chat_id=-100123,
    )

    candidate = TelegramCandidateBuilder().build(parsed)

    assert parsed.marketplace == "amazon"
    assert parsed.discount_visible == 30.0
    assert parsed.written_price == 11549.0
    assert candidate.internal_classification == LISTENER_DEAL
    assert candidate.outbox_item is not None
    assert candidate.outbox_item.message_payload["current_price"] == 11549.0
    assert candidate.outbox_item.message_payload["requires_live_validation"] is True



# ---------------------------------------------------------------------------
# Test obligatorio: links ML desde Telegram NO generan affiliate
# ---------------------------------------------------------------------------


def test_ml_telegram_source_links_remain_ignored_no_affiliate():
    """Los links ML que llegan por Telegram nunca van al ML hunter, por lo
    que nunca generan affiliate_url. El candidato queda en `ignored`.

    Esto demuestra que la regla 'ML desde Telegram = ignorar' opera ANTES
    del flujo afiliado: no hay riesgo de que un link de Telegram termine
    publicado con afiliado del bot.
    """
    parsed = _parse_fixture("mercadolibre_link_should_be_ignored.txt")
    candidate = TelegramCandidateBuilder().build(parsed)
    assert candidate.internal_classification == LISTENER_IGNORED_ML
    assert candidate.outbox_item is None
    # Y el signal nunca se construye (porque el gate ML actúa primero).
    assert candidate.signal is None
