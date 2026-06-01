"""Adapter `legacy → ofertas_hunter`.

Convierte el resultado del worker legacy (`LegacyFetchResult.data` que es un
`dict` con la nomenclatura del scraper original) a los modelos del bot
nuevo (`ExtractedProduct`, `Product`, `Offer`, `OutboxItem`) y aplica el
`PriceErrorScorer` actual con todas sus salvaguardas.

Reglas del adapter (Criterio D + E de la spec):
- No encola si falta `image_url`, `current_price` o `url` (gates duros).
- No usa el verdict legacy (`EXCELENTE/BUENA/REGULAR/DESCARTAR`) como
  fuente de verdad — solo alimenta `PriceErrorScorer`.
- Registra razón de descarte en `discarded_candidates` y emite
  `runtime_event` cuando el item no pasa los gates.
- Si `previous_price` no viene del DOM, queda `None` y el scorer lo
  trata como tal (no inventamos `previous_price`).

Interfaz pública:

    process_legacy_fetch_result(
        db_conn, scorer, fetch_result, *, normal_offer_min_discount=50.0
    ) -> AdapterOutcome
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ...intelligence.price_error_scorer import PriceErrorScorer
from ...marketplaces.base import ExtractedProduct
from ...models import (
    Classification,
    OutboxState,
    OutboxType,
    PriceErrorSignal,
    Source,
)
from .worker import LegacyFetchResult


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resultado tipado del adapter (para tests + LegacyAmazonHunterAgent)
# ---------------------------------------------------------------------------


@dataclass
class AdapterOutcome:
    """Resumen de qué hizo el adapter con un `LegacyFetchResult`."""

    url: str
    final_url: str
    enqueued_outbox_id: Optional[int] = None
    discarded_reason: Optional[str] = None
    classification: Optional[str] = None
    score: Optional[int] = None
    captcha: bool = False
    captcha_confidence: Optional[str] = None
    captcha_signals: tuple[str, ...] = field(default_factory=tuple)
    extracted: Optional[ExtractedProduct] = None


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonicalize(url: str) -> str:
    """Quita query string si la URL tiene `/dp/<ASIN>` o normaliza listings."""
    if not url:
        return url
    m = re.search(r"/dp/([A-Z0-9]{10})", url)
    if m:
        return f"https://www.amazon.com.mx/dp/{m.group(1)}"
    # Listings: quitar refs y query trackers
    base = url.split("?")[0]
    return base


def _coerce_in_stock(availability: Optional[str]) -> Optional[bool]:
    """Convierte texto de availability del legacy a bool."""
    if not availability:
        return None
    a = availability.lower()
    if any(k in a for k in ("agotado", "no disponible", "out of stock", "currently unavailable")):
        return False
    if any(k in a for k in ("en stock", "disponible", "in stock", "envío gratis")):
        return True
    # Texto presente pero ambiguo → asumimos True (Amazon suele mostrar
    # availability sólo cuando hay producto). El gate dispatcher de stock
    # lo valida una vez más.
    return True


def _build_extracted_product(data: dict[str, Any], original_url: str) -> ExtractedProduct:
    """Convierte el dict legacy a un `ExtractedProduct`.

    `previous_price` queda `None` si el legacy no encontró un precio anterior
    VERIFICADO: el scorer no aplicará bonus `previous_vs_current_drop_*`, y el
    item no podrá publicarse como oferta de descuento (correcto).
    """
    canonical = _canonicalize(data.get("url") or original_url)
    old_price_verified = bool(data.get("old_price_verified"))
    # Si el precio anterior no está verificado, NO lo propagamos: nunca
    # inventamos "Antes".
    previous_price = data.get("price_original") if old_price_verified else None
    discount_percent = (
        data.get("discount_percent")
        if data.get("discount_percent_verified")
        else None
    )
    return ExtractedProduct(
        url=original_url,
        canonical_url=canonical,
        marketplace="amazon",
        title=data.get("title"),
        current_price=data.get("price_current"),
        previous_price=previous_price,
        discount_percent=discount_percent,
        calculated_discount_percent=(
            discount_percent
            if data.get("discount_source") == "calculated_verified"
            else None
        ),
        image_url=data.get("image_url"),
        availability=data.get("availability"),
        in_stock=_coerce_in_stock(data.get("availability")),
        condition="new",
        brand_guess=data.get("brand"),
        category_guess=data.get("category"),
        asin=data.get("asin"),
        raw_price_text=None,
        raw_previous_price_text=None,
        extraction_confidence="medium",
        extraction_warnings=[],
        is_monthly_payment=False,
        is_publishable=True,  # los gates del adapter aplican aparte
        not_publishable_reasons=[],
    )


def _build_signal(extracted: ExtractedProduct) -> PriceErrorSignal:
    return PriceErrorSignal(
        product_title=extracted.title or "(sin título)",
        marketplace=extracted.marketplace,
        source=Source.AMAZON_HUNTER.value,
        original_url=extracted.url,
        resolved_url=extracted.canonical_url,
        current_price=extracted.current_price,
        previous_price=extracted.previous_price,
        discount_percent=extracted.discount_percent,
        brand=extracted.brand_guess,
        category=extracted.category_guess,
        condition=extracted.condition,
        has_stock=extracted.in_stock,
        has_image=bool(extracted.image_url),
    )


def _decide_outbox_type(
    extracted: ExtractedProduct, scoring, *, normal_offer_min_discount: float
) -> Optional[str]:
    """Devuelve el `outbox_type` (`price_error` | `normal`) o `None` si no aplica."""
    if scoring.classification in (
        Classification.PRICE_ERROR_CONFIRMED.value,
        Classification.POSSIBLE_PRICE_ERROR.value,
    ):
        return OutboxType.PRICE_ERROR.value
    if (
        extracted.discount_percent is not None
        and extracted.discount_percent >= normal_offer_min_discount
    ):
        return OutboxType.NORMAL.value
    return None


# ---------------------------------------------------------------------------
# Persistencia (mismo schema que el AmazonHunterAgent del bot nuevo)
# ---------------------------------------------------------------------------


def _upsert_product(db: sqlite3.Connection, product: ExtractedProduct) -> int:
    existing = db.execute(
        "SELECT id FROM products WHERE url_canonical = ?", (product.canonical_url,)
    ).fetchone()
    if existing is not None:
        db.execute(
            "UPDATE products SET last_seen_at = ?, image_url = COALESCE(?, image_url), "
            "title = COALESCE(?, title), brand = COALESCE(?, brand), "
            "category = COALESCE(?, category) WHERE id = ?",
            (
                _now_iso(),
                product.image_url,
                product.title,
                product.brand_guess,
                product.category_guess,
                existing["id"],
            ),
        )
        return existing["id"]
    cur = db.execute(
        "INSERT INTO products (marketplace, marketplace_id, url_canonical, title, brand, "
        "category, condition, image_url, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            product.marketplace,
            product.asin,
            product.canonical_url,
            product.title or "(sin título)",
            product.brand_guess,
            product.category_guess,
            product.condition,
            product.image_url,
            _now_iso(),
            _now_iso(),
        ),
    )
    return cur.lastrowid


def _save_price_observation(
    db: sqlite3.Connection, product_id: int, product: ExtractedProduct
) -> Optional[int]:
    cur = db.execute(
        "INSERT INTO price_observations (product_id, current_price, previous_price, "
        "currency, discount_percent, has_stock, source, raw_signals_json, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            product_id,
            product.current_price,
            product.previous_price,
            "MXN",
            product.discount_percent,
            int(product.in_stock) if product.in_stock is not None else None,
            Source.AMAZON_HUNTER.value,
            json.dumps(
                {
                    "extractor": "legacy_amazon",
                    "extraction_confidence": product.extraction_confidence,
                },
                ensure_ascii=False,
            ),
            _now_iso(),
        ),
    )
    return cur.lastrowid


def _upsert_offer(db: sqlite3.Connection, product_id: int, scoring) -> int:
    cur = db.execute(
        "INSERT INTO offers (product_id, classification, score, reasons_json, "
        "discount_percent, state, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            product_id,
            scoring.classification,
            scoring.score,
            json.dumps(list(scoring.reasons), ensure_ascii=False),
            None,
            "eligible",
            _now_iso(),
            _now_iso(),
        ),
    )
    return cur.lastrowid


def _recently_published(db: sqlite3.Connection, asin: str, hours: int = 48) -> bool:
    if not asin:
        return False
    row = db.execute(
        """
        SELECT 1
        FROM outbox o
        JOIN offers   of ON of.id = o.offer_id
        JOIN products p  ON p.id  = of.product_id
        WHERE p.marketplace_id = ?
          AND o.state = 'sent'
          AND o.last_attempt_at >= datetime('now', ?)
        LIMIT 1
        """,
        (asin, f"-{int(hours)} hours"),
    ).fetchone()
    return row is not None


def _enqueue_outbox(
    db: sqlite3.Connection,
    offer_id: int,
    extracted: ExtractedProduct,
    scoring,
    outbox_type: str,
    data: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    if extracted.asin and _recently_published(db, extracted.asin, hours=48):
        logger.info(
            "legacy_adapter: ASIN %s publicado hace <48h, saltando", extracted.asin
        )
        return None

    data = data or {}
    affiliate_url = data.get("affiliate_url")
    payload = {
        "title": extracted.title,
        "current_price": extracted.current_price,
        "previous_price": extracted.previous_price,
        "discount_percent": extracted.discount_percent,
        "image_url": extracted.image_url,
        "url": affiliate_url or extracted.canonical_url,
        "canonical_url": extracted.canonical_url,
        "marketplace": extracted.marketplace,
        "brand": extracted.brand_guess,
        "category": extracted.category_guess,
        "asin": extracted.asin,
        "confidence_label": scoring.confidence_label,
        "score": scoring.score,
        "score_classification": scoring.classification,
        "in_stock": extracted.in_stock,
        "source": Source.AMAZON_HUNTER.value,
        "requires_live_validation": False,
        "extractor": "legacy_amazon",
        # Trazabilidad de precio/afiliado (corrección de descuentos falsos).
        "old_price": extracted.previous_price,
        "old_price_source": data.get("old_price_source"),
        "old_price_verified": bool(data.get("old_price_verified")),
        "discount_percent_verified": bool(data.get("discount_percent_verified")),
        "current_price_source": data.get("current_price_source"),
        "current_price_raw_text": data.get("current_price_raw_text"),
        "current_price_is_unit_price": bool(data.get("current_price_is_unit_price")),
        "current_price_verified": bool(
            data.get("current_price_source")
            and not data.get("current_price_is_unit_price")
        ),
        "extreme_discount_verified": bool(data.get("extreme_discount_verified")),
        "affiliate_url": affiliate_url,
        "affiliate_valid": bool(
            affiliate_url and ("amzn.to/" in affiliate_url or "tag=" in affiliate_url)
        ),
        "validation_errors": data.get("validation_errors") or [],
        "reject_reason": None,
    }
    cur = db.execute(
        "INSERT INTO outbox (offer_id, type, enqueued_at, scheduled_for, attempts, "
        "last_attempt_at, state, message_payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            offer_id,
            outbox_type,
            _now_iso(),
            None,
            0,
            None,
            OutboxState.PENDING.value,
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    return cur.lastrowid


def _save_discard(
    db: sqlite3.Connection, *, url: str, reason: str, payload: dict
) -> None:
    db.execute(
        "INSERT INTO discarded_candidates (source, raw_payload_json, reason, created_at) "
        "VALUES (?, ?, ?, ?)",
        (
            Source.AMAZON_HUNTER.value,
            json.dumps({"url": url, "extractor": "legacy_amazon", **payload}, ensure_ascii=False),
            reason,
            _now_iso(),
        ),
    )


def _emit_runtime_event(
    db: sqlite3.Connection, *, kind: str, severity: str, payload: dict
) -> None:
    db.execute(
        "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (kind, severity, json.dumps(payload, ensure_ascii=False), _now_iso()),
    )


# ---------------------------------------------------------------------------
# API pública del adapter
# ---------------------------------------------------------------------------


def process_legacy_fetch_result(
    db: sqlite3.Connection,
    scorer: PriceErrorScorer,
    fetch_result: LegacyFetchResult,
    *,
    original_url: str,
    normal_offer_min_discount: float = 50.0,
) -> AdapterOutcome:
    """Orquesta gates + scoring + persistencia para un único fetch.

    Reglas (criterio D + E de la spec):
    - Si `fetch_result.captcha` con confidence high → marca captcha.
      No persiste nada como producto. El caller decide si pausa.
    - Si `fetch_result.captcha` con confidence medium → registra
      runtime_event de baja severidad (`amazon_legacy_captcha_suspect`)
      pero NO encola y NO ordena pausa (criterio C).
    - Si `not fetch_result.ok` y reason `dom_incomplete` → registra
      `discarded_candidates` con reason `dom_incomplete`. NO marca
      captcha.
    - Si pasa hasta acá pero el `dict` no tiene `image_url`, `title` o
      `current_price` → discard.
    - Si pasa, scorea con `PriceErrorScorer` (mismas reglas que el
      bot nuevo: anti-accesorio, anti-audífonos, premium audio,
      smartphone-no-incluye-buds).
    - Construye signal + scoring + decide outbox_type.
    - Persiste product/observation/offer/outbox y devuelve
      `AdapterOutcome` con `enqueued_outbox_id`.
    """
    outcome = AdapterOutcome(url=original_url, final_url=fetch_result.final_url or original_url)

    # 1) Captcha real high-confidence: el caller debe pausar marketplace.
    if fetch_result.captcha and fetch_result.captcha_confidence == "high":
        outcome.captcha = True
        outcome.captcha_confidence = "high"
        outcome.captcha_signals = fetch_result.captcha_signals
        outcome.discarded_reason = "captcha_high_confidence"
        _emit_runtime_event(
            db,
            kind="amazon_legacy_captcha_confirmed",
            severity="error",
            payload={
                "url": original_url,
                "final_url": fetch_result.final_url,
                "signals": list(fetch_result.captcha_signals),
                "confidence": "high",
            },
        )
        db.commit()
        return outcome

    # 2) Captcha medium/sospecha (criterio C): NO pausa. Registra y descarta
    #    como item, pero el orquestador no debe interpretar esto como
    #    "captcha real ahora".
    if fetch_result.captcha and fetch_result.captcha_confidence == "medium":
        outcome.captcha = True
        outcome.captcha_confidence = "medium"
        outcome.captcha_signals = fetch_result.captcha_signals
        outcome.discarded_reason = "captcha_medium_confidence_not_paused"
        _emit_runtime_event(
            db,
            kind="amazon_legacy_captcha_suspect",
            severity="warning",
            payload={
                "url": original_url,
                "final_url": fetch_result.final_url,
                "signals": list(fetch_result.captcha_signals),
                "confidence": "medium",
                "note": "no_pause_legacy_distinguishes_dom_incomplete",
            },
        )
        db.commit()
        return outcome

    # 3) Fetch falló pero no es captcha (DOM incompleto, 503 final, etc.)
    if not fetch_result.ok:
        reason = fetch_result.reason or "fetch_failed"
        outcome.discarded_reason = reason
        _save_discard(
            db,
            url=original_url,
            reason=reason,
            payload={
                "final_url": fetch_result.final_url,
                "status": fetch_result.status,
            },
        )
        db.commit()
        return outcome

    # 4) OK pero el dict puede no tener todos los campos.
    data = fetch_result.data or {}
    extracted = _build_extracted_product(data, original_url)
    outcome.extracted = extracted

    # Gates duros (criterio D): imagen + precio actual + url canónica.
    missing: list[str] = []
    if not extracted.image_url:
        missing.append("image_url")
    if extracted.current_price is None:
        missing.append("current_price")
    if not extracted.canonical_url:
        missing.append("url")
    if not extracted.title:
        missing.append("title")

    if missing:
        reason = f"missing_required_fields:{','.join(missing)}"
        outcome.discarded_reason = reason
        _save_discard(
            db,
            url=original_url,
            reason=reason,
            payload={
                "missing": missing,
                "data_keys": list(data.keys()),
            },
        )
        db.commit()
        return outcome

    # 5) Scoring con el PriceErrorScorer del bot nuevo (no el legacy verdict).
    signal = _build_signal(extracted)
    scoring = scorer.score(signal)
    outcome.classification = scoring.classification
    outcome.score = scoring.score

    # 6) Decidir outbox_type.
    outbox_type = _decide_outbox_type(
        extracted, scoring, normal_offer_min_discount=normal_offer_min_discount
    )
    if outbox_type is None:
        outcome.discarded_reason = "no_outbox_type_below_thresholds"
        _save_discard(
            db,
            url=original_url,
            reason="below_thresholds",
            payload={
                "classification": scoring.classification,
                "score": scoring.score,
                "discount_percent": extracted.discount_percent,
            },
        )
        db.commit()
        return outcome

    # 7) Persistir.
    product_id = _upsert_product(db, extracted)
    _save_price_observation(db, product_id, extracted)
    offer_id = _upsert_offer(db, product_id, scoring)

    enqueued_id = _enqueue_outbox(db, offer_id, extracted, scoring, outbox_type, data)
    if enqueued_id is None:
        outcome.discarded_reason = "recently_published"
    else:
        outcome.enqueued_outbox_id = enqueued_id

    db.commit()
    return outcome
