"""PlaywrightRevalidator: revalida items del outbox contra la página real.

Implementa la interfaz `Revalidator` del dispatcher (Fase 3.1). El dispatcher
llama a `revalidate(item)` y actúa según `RevalidationResult`:

- `still_eligible=True` + `payload` actualizado → publica con el payload nuevo.
- `still_eligible=False` + `discard_reason` → descarta.

Reglas (spec §8 + §16):

- Si item viene de Telegram (`source=telegram`) → siempre revalidar.
- Si item lleva > 1h en outbox → el dispatcher ya invoca al revalidator antes
  de pickearlo (esa lógica vive en `OutboxDispatcher`).
- Si revalidación confirma `price_error_confirmed` → permite bypass cooldown.
- Si revalidación confirma `normal_offer >= 50%` → entra a outbox normal.
- Nunca publicar sin imagen, precio, url, stock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..browser.browser_context import BrowserWorker, RenderedPage
from ..dispatching.dispatcher import RevalidationResult, Revalidator
from ..extraction.amazon_product_parser import AmazonProductParser
from ..extraction.mercadolibre_product_parser import (
    MercadoLibreProductParser,
    is_mercadolibre_url,
)
from ..intelligence.price_error_scorer import PriceErrorScorer
from ..marketplaces.base import ExtractedProduct
from ..marketplaces.url_utils import canonicalize_amazon_url, is_amazon_url
from ..models import (
    Classification,
    OutboxItem,
    OutboxType,
    PriceErrorSignal,
    Source,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resultado interno detallado (auditable en SQLite/logs)
# ---------------------------------------------------------------------------


@dataclass
class RevalidationDetail:
    ok: bool
    classification: str
    fatal_reason: Optional[str]
    reasons: list[str]
    extracted: Optional[ExtractedProduct]
    snapshot_saved: Optional[int]  # id de dom_snapshots
    suggested_outbox_type: str
    confidence_label: str


# ---------------------------------------------------------------------------
# PlaywrightRevalidator
# ---------------------------------------------------------------------------


class PlaywrightRevalidator(Revalidator):
    """Revalidator que usa BrowserWorker + parser específico por marketplace."""

    def __init__(
        self,
        browser: BrowserWorker,
        *,
        amazon_parser: Optional[AmazonProductParser] = None,
        mercadolibre_parser: Optional[MercadoLibreProductParser] = None,
        scorer: Optional[PriceErrorScorer] = None,
        normal_offer_min_discount: float = 50.0,
        db_conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        self.browser = browser
        self.amazon_parser = amazon_parser or AmazonProductParser()
        self.mercadolibre_parser = mercadolibre_parser or MercadoLibreProductParser()
        self.scorer = scorer or PriceErrorScorer()
        self.normal_offer_min_discount = normal_offer_min_discount
        self.db = db_conn

    # ------------------------------------------------------------------
    # API: Revalidator
    # ------------------------------------------------------------------

    async def revalidate(self, item: OutboxItem) -> RevalidationResult:
        """Implementa la interfaz `Revalidator` del dispatcher."""
        detail = await self.revalidate_detailed(item)
        if not detail.ok:
            return RevalidationResult(
                still_eligible=False,
                discard_reason=detail.fatal_reason or "no_longer_eligible",
            )
        # Construimos payload nuevo respetando el shape del WhatsAppPublisher.
        payload = self._build_payload(item, detail)
        return RevalidationResult(
            still_eligible=True,
            payload=payload,
        )

    # ------------------------------------------------------------------
    # API extendida (para tests + scripts)
    # ------------------------------------------------------------------

    async def revalidate_detailed(self, item: OutboxItem) -> RevalidationDetail:
        url = self._url_to_fetch(item)
        if not url:
            return self._fatal("no_url", "no_longer_eligible")

        page = await self.browser.fetch(url)

        if not page.ok:
            snapshot_id = self._maybe_save_snapshot(item, url, page, reason="fetch_failed")
            fatal = "captcha_detected" if page.blocked else (page.error or "fetch_failed")
            return RevalidationDetail(
                ok=False,
                classification="no_longer_eligible",
                fatal_reason=fatal,
                reasons=[fatal],
                extracted=None,
                snapshot_saved=snapshot_id,
                suggested_outbox_type=item.type,
                confidence_label="low",
            )

        product = self._parse(item, page)

        if not product.is_publishable:
            snapshot_id = self._maybe_save_snapshot(
                item, url, page, reason="not_publishable"
            )
            fatal = product.not_publishable_reasons[0] if product.not_publishable_reasons else "not_publishable"
            return RevalidationDetail(
                ok=False,
                classification="not_publishable",
                fatal_reason=fatal,
                reasons=list(product.not_publishable_reasons),
                extracted=product,
                snapshot_saved=snapshot_id,
                suggested_outbox_type=item.type,
                confidence_label=product.extraction_confidence,
            )

        # Construir signal y scorear
        signal = self._build_signal(item, product)
        scoring = self.scorer.score(signal)

        # Decidir si sigue siendo elegible.
        suggested_outbox_type = self._decide_outbox_type(item, product, scoring)
        if suggested_outbox_type is None:
            return RevalidationDetail(
                ok=False,
                classification="no_longer_eligible",
                fatal_reason="discount_below_threshold_after_revalidation",
                reasons=list(scoring.reasons),
                extracted=product,
                snapshot_saved=None,
                suggested_outbox_type=item.type,
                confidence_label=scoring.confidence_label,
            )

        return RevalidationDetail(
            ok=True,
            classification=scoring.classification,
            fatal_reason=None,
            reasons=list(scoring.reasons),
            extracted=product,
            snapshot_saved=None,
            suggested_outbox_type=suggested_outbox_type,
            confidence_label=scoring.confidence_label,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _url_to_fetch(self, item: OutboxItem) -> Optional[str]:
        payload = item.message_payload or {}
        for key in ("resolved_url", "url", "original_url"):
            value = payload.get(key)
            if value:
                return value
        return None

    def _parse(self, item: OutboxItem, page: RenderedPage) -> ExtractedProduct:
        url = page.final_url or page.url
        marketplace = (item.message_payload or {}).get("marketplace") or _infer_marketplace(url)
        expected_title = (item.message_payload or {}).get("title")

        if marketplace == "amazon" or is_amazon_url(url):
            return self.amazon_parser.parse(
                page.html, url, expected_title=expected_title
            )

        if marketplace == "mercadolibre" or is_mercadolibre_url(url):
            # Regla dura: si la fuente es 'telegram', NO procesamos ML.
            payload = item.message_payload or {}
            if payload.get("source") == Source.TELEGRAM.value:
                product = ExtractedProduct(
                    url=url,
                    canonical_url=url,
                    marketplace="mercadolibre",
                )
                product.not_publishable_reasons.append("mercadolibre_link_from_telegram")
                product.extraction_warnings.append("blocked_by_rule")
                return product
            return self.mercadolibre_parser.parse(
                page.html, url, expected_title=expected_title
            )

        # Marketplace no soportado todavía
        product = ExtractedProduct(
            url=url,
            canonical_url=url,
            marketplace=marketplace,
        )
        product.not_publishable_reasons.append("marketplace_parser_not_implemented")
        product.extraction_warnings.append(f"marketplace={marketplace}")
        return product

    def _build_signal(self, item: OutboxItem, product: ExtractedProduct) -> PriceErrorSignal:
        payload = item.message_payload or {}
        return PriceErrorSignal(
            product_title=product.title or "(sin título)",
            marketplace=product.marketplace,
            source=payload.get("source") or Source.AMAZON_HUNTER.value,
            original_url=payload.get("original_url") or product.url,
            source_channel=payload.get("source_channel"),
            original_text=payload.get("original_text"),
            resolved_url=product.canonical_url,
            current_price=product.current_price,
            previous_price=product.previous_price,
            discount_percent=product.discount_percent,
            brand=product.brand_guess,
            category=product.category_guess,
            condition=product.condition,
            has_stock=product.in_stock,
            has_image=bool(product.image_url),
            urgency_terms=list(payload.get("urgency_terms") or []),
            telegram_confidence=float(payload.get("telegram_confidence") or 0),
            monthly_payment_suspected=product.is_monthly_payment,
            variant_mismatch=("variant_mismatch" in product.not_publishable_reasons),
            created_at=datetime.now(timezone.utc),
        )

    def _decide_outbox_type(
        self,
        item: OutboxItem,
        product: ExtractedProduct,
        scoring,
    ) -> Optional[str]:
        # Confirmado o posible price_error → bypass cooldown
        if scoring.classification in (
            Classification.PRICE_ERROR_CONFIRMED.value,
            Classification.POSSIBLE_PRICE_ERROR.value,
        ):
            return OutboxType.PRICE_ERROR.value

        # Oferta normal con descuento real >= 50%
        if (
            product.discount_percent is not None
            and product.discount_percent >= self.normal_offer_min_discount
        ):
            return OutboxType.NORMAL.value

        # Si el item venía como price_error pero en página real es normal_offer
        # con descuento alto, mantenerlo como PE no aplica. Si era normal y
        # tiene descuento >= 50, sigue como NORMAL.
        if (
            item.type == OutboxType.NORMAL.value
            and product.discount_percent is not None
            and product.discount_percent >= self.normal_offer_min_discount
        ):
            return OutboxType.NORMAL.value

        return None

    def _build_payload(self, item: OutboxItem, detail: RevalidationDetail) -> dict:
        product = detail.extracted
        assert product is not None
        original = dict(item.message_payload or {})

        # canonical_url: del producto recién extraído (es la fuente de verdad).
        canonical_url = product.canonical_url

        # affiliate_url y campos relacionados se conservan del payload original.
        # La revalidación NO regenera el modal Compartir (eso es trabajo del
        # hunter, requiere navegación adicional). Si el payload original NO
        # tenía affiliate_url, queda en None — el publisher decidirá.
        affiliate_url = original.get("affiliate_url")
        affiliate_product_id = original.get("affiliate_product_id")
        commission_text = original.get("commission_text")

        # `url` = lo que se publica: prioriza affiliate_url cuando existe.
        publish_url = affiliate_url or canonical_url

        original.update(
            {
                "title": product.title,
                "current_price": product.current_price,
                "previous_price": product.previous_price,
                "discount_percent": product.discount_percent,
                "image_url": product.image_url,
                "url": publish_url,
                "canonical_url": canonical_url,
                "affiliate_url": affiliate_url,
                "affiliate_product_id": affiliate_product_id,
                "commission_text": commission_text,
                "marketplace": product.marketplace,
                "confidence_label": detail.confidence_label,
                "in_stock": product.in_stock,
                "asin": product.asin,
                "revalidated_at": datetime.now(timezone.utc).isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z"),
                "score_classification": detail.classification,
                "requires_live_validation": False,  # ya revalidado
            }
        )
        original["_suggested_outbox_type"] = detail.suggested_outbox_type
        return original

    def _maybe_save_snapshot(
        self,
        item: OutboxItem,
        url: str,
        page: RenderedPage,
        *,
        reason: str,
    ) -> Optional[int]:
        if self.db is None:
            return None
        try:
            cur = self.db.execute(
                "INSERT INTO dom_snapshots (marketplace, context, url, content, captured_at, reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (item.message_payload or {}).get("marketplace") or "unknown",
                    "revalidator",
                    url,
                    page.html or "",
                    datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
                        "+00:00", "Z"
                    ),
                    reason,
                ),
            )
            return cur.lastrowid
        except sqlite3.Error as exc:
            logger.warning("dom_snapshots insert failed: %s", exc)
            return None

    def _fatal(self, fatal: str, classification: str) -> RevalidationDetail:
        return RevalidationDetail(
            ok=False,
            classification=classification,
            fatal_reason=fatal,
            reasons=[fatal],
            extracted=None,
            snapshot_saved=None,
            suggested_outbox_type="discard",
            confidence_label="low",
        )


# ---------------------------------------------------------------------------
# Helpers libres
# ---------------------------------------------------------------------------


def _infer_marketplace(url: str) -> str:
    if not url:
        return "other"
    if is_amazon_url(url):
        return "amazon"
    if is_mercadolibre_url(url):
        return "mercadolibre"
    return "other"
