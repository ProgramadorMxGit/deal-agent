"""Agente Mercado Libre Hunter (versión inicial Fase 3.4).

Igual que `AmazonHunterAgent` pero:

- Usa `MercadoLibreSession` (cookies) y registra estado de cookies en
  `runtime_events`.
- Usa `MercadoLibreProductParser`.
- Detecta redirect a `/account-verification` o `/login` y emite evento
  `runtime_events(severity=critical, kind=cookie_expiry)`, pausando el agente.
- Guarda cookies actualizadas al cerrar si `save_cookies_on_exit=True`.
- **Source = "mercadolibre_hunter"** (NO `telegram`), por eso pasa la regla
  dura de "ignorar ML desde Telegram": esa regla solo aplica a
  `source=telegram`. El hunter propio sí explora ML.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..browser.browser_context import BrowserWorker, RenderedPage
from ..extraction.mercadolibre_product_parser import (
    MercadoLibreProductParser,
    has_share_button,
)
from ..intelligence.price_error_scorer import PriceErrorScorer
from ..marketplaces.base import ExtractedProduct
from ..marketplaces.mercadolibre_affiliate import (
    AffiliateExtractor,
    AffiliateInfo,
)
from ..models import (
    Classification,
    OutboxState,
    OutboxType,
    PriceErrorSignal,
    Source,
)
from ..session.mercadolibre_session import MercadoLibreSession, is_login_redirect


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resultado por URL
# ---------------------------------------------------------------------------


@dataclass
class MlHuntOutcome:
    url: str
    final_url: str
    extracted: Optional[ExtractedProduct]
    classification: str
    suggested_outbox_type: Optional[str]
    enqueued_outbox_id: Optional[int]
    discarded_reason: Optional[str]
    snapshot_id: Optional[int] = None
    paused_for_login: bool = False
    affiliate_url: Optional[str] = None
    affiliate_product_id: Optional[str] = None
    affiliate_error: Optional[str] = None


# ---------------------------------------------------------------------------
# Agente
# ---------------------------------------------------------------------------


class MercadoLibreHunterAgent:
    """Hunter Mercado Libre. Recibe URLs y crea offers/outbox."""

    def __init__(
        self,
        browser: BrowserWorker,
        *,
        session: Optional[MercadoLibreSession] = None,
        db_conn: Optional[sqlite3.Connection] = None,
        parser: Optional[MercadoLibreProductParser] = None,
        scorer: Optional[PriceErrorScorer] = None,
        normal_offer_min_discount: float = 50.0,
        save_cookies_on_exit: bool = True,
        affiliate_extractor: Optional[AffiliateExtractor] = None,
        affiliate_required_for_publish: bool = True,
        session_manager: Any = None,
    ) -> None:
        self.browser = browser
        self.session = session
        self.db = db_conn
        self.parser = parser or MercadoLibreProductParser()
        self.scorer = scorer or PriceErrorScorer()
        self.normal_offer_min_discount = normal_offer_min_discount
        self.save_cookies_on_exit = save_cookies_on_exit
        self.affiliate_extractor = affiliate_extractor
        self.affiliate_required_for_publish = affiliate_required_for_publish
        self.session_manager = session_manager
        self._paused = False

    async def aclose(self) -> None:
        try:
            await self.browser.aclose()
        except Exception:
            pass

    @property
    def paused(self) -> bool:
        return self._paused

    def _session_manager_blocks(self) -> Optional[str]:
        """Devuelve el estado del manager si NO es VALID; si no hay manager
        o el estado es VALID, devuelve None.
        """
        manager = self.session_manager
        if manager is None:
            return None
        try:
            status = getattr(manager, "status", None)
            if status is None:
                return None
            value = getattr(status, "value", None) or str(status)
            if value == "valid":
                return None
            return value
        except Exception:
            return None

    # Throttle: emitir el evento de skip máximo 1 vez por minuto
    _last_skip_event_ts: float = 0.0

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    async def hunt_urls(
        self, urls: list[str], *, max_urls: Optional[int] = None
    ) -> list[MlHuntOutcome]:
        # Gate: si hay session_manager y está en estado != VALID,
        # saltamos el ciclo entero. Amazon y Telegram NO se ven
        # afectados (corren en otros agentes).
        manager_state = self._session_manager_blocks()
        if manager_state is not None:
            import time as _time
            now_ts = _time.monotonic()
            if now_ts - self._last_skip_event_ts >= 60.0:
                self._last_skip_event_ts = now_ts
                self._emit_runtime_event(
                    kind="ml_hunt_skipped_session_invalid",
                    severity="warning",
                    payload={"manager_state": manager_state, "urls": len(urls)},
                )
            return []

        target = urls[: max_urls] if max_urls else urls
        outcomes: list[MlHuntOutcome] = []
        for url in target:
            if self._paused:
                logger.warning("ml_hunter pausado; saltando %s", url)
                break
            outcome = await self.hunt_one(url)
            outcomes.append(outcome)
            if outcome.paused_for_login:
                self._paused = True
                self._emit_runtime_event(
                    kind="cookie_expiry",
                    severity="critical",
                    payload={"url": url, "final_url": outcome.final_url},
                )
                # Notificar al manager que la sesión está inválida.
                manager = self.session_manager
                if manager is not None and hasattr(manager, "mark_invalid"):
                    try:
                        manager.mark_invalid("login_redirect_during_hunt")
                    except Exception:
                        logger.exception("session_manager.mark_invalid falló")
                break
        return outcomes

    async def hunt_from_frontier(self, *, max_urls: int = 5) -> list[MlHuntOutcome]:
        """Toma URLs `kind=product` del frontier ML."""
        if self.db is None or self._paused:
            return []
        # Gate por estado del manager (mismo criterio que hunt_urls).
        manager_state = self._session_manager_blocks()
        if manager_state is not None:
            import time as _time
            now_ts = _time.monotonic()
            if now_ts - self._last_skip_event_ts >= 60.0:
                self._last_skip_event_ts = now_ts
                self._emit_runtime_event(
                    kind="ml_hunt_skipped_session_invalid",
                    severity="warning",
                    payload={"manager_state": manager_state, "from_frontier": True},
                )
            return []

        from ..exploration.frontier import FrontierRepo

        frontier = FrontierRepo(self.db)
        items = frontier.pop("mercadolibre", kind="product", limit=max_urls)
        if not items:
            return []
        outcomes: list[MlHuntOutcome] = []
        for item in items:
            if self._paused:
                break
            try:
                outcome = await self.hunt_one(item.url)
            except Exception as exc:
                logger.warning("hunt_one raised on %s: %s", item.url, exc)
                continue
            outcomes.append(outcome)
        return outcomes

    async def hunt_one(self, url: str) -> MlHuntOutcome:
        logger.info("ml_hunter fetch %s", url)
        page = await self.browser.fetch(url)

        # Detección de login wall.
        if is_login_redirect(page.final_url):
            snapshot_id = self._save_snapshot(url, page, reason="login_redirect")
            self._save_discard(
                url=url,
                reason="login_redirect",
                payload={"final_url": page.final_url},
            )
            return MlHuntOutcome(
                url=url,
                final_url=page.final_url,
                extracted=None,
                classification="login_redirect",
                suggested_outbox_type=None,
                enqueued_outbox_id=None,
                discarded_reason="login_redirect",
                snapshot_id=snapshot_id,
                paused_for_login=True,
            )

        if not page.ok:
            snapshot_id = self._save_snapshot(url, page, reason="fetch_failed")
            self._mark_visited(url)
            reason = "captcha_detected" if page.blocked else (page.error or "fetch_failed")
            self._save_discard(
                url=url,
                reason=reason,
                payload={"final_url": page.final_url, "status": page.status},
            )
            paused = page.blocked
            if paused:
                self._emit_runtime_event(
                    kind="captcha",
                    severity="error",
                    payload={"url": url, "final_url": page.final_url},
                )
            return MlHuntOutcome(
                url=url,
                final_url=page.final_url,
                extracted=None,
                classification="fetch_failed",
                suggested_outbox_type=None,
                enqueued_outbox_id=None,
                discarded_reason=reason,
                snapshot_id=snapshot_id,
                paused_for_login=paused,
            )

        product = self.parser.parse(page.html, page.final_url)
        product_id = self._upsert_product(product)
        self._save_price_observation(product_id, product)
        self._mark_visited(product.canonical_url)

        if not product.is_publishable:
            snapshot_id = self._save_snapshot(url, page, reason="not_publishable")
            reason = product.not_publishable_reasons[0] if product.not_publishable_reasons else "not_publishable"
            self._save_discard(
                url=product.canonical_url,
                reason=reason,
                payload={"reasons": product.not_publishable_reasons},
            )
            return MlHuntOutcome(
                url=url,
                final_url=page.final_url,
                extracted=product,
                classification="not_publishable",
                suggested_outbox_type=None,
                enqueued_outbox_id=None,
                discarded_reason=reason,
                snapshot_id=snapshot_id,
            )

        signal = self._build_signal(product)
        scoring = self.scorer.score(signal)
        outbox_type = self._decide_outbox_type(product, scoring)
        if outbox_type is None:
            self._save_discard(
                url=product.canonical_url,
                reason="discount_below_threshold",
                payload={"score": scoring.score, "reasons": list(scoring.reasons)},
            )
            return MlHuntOutcome(
                url=url,
                final_url=page.final_url,
                extracted=product,
                classification=scoring.classification,
                suggested_outbox_type=None,
                enqueued_outbox_id=None,
                discarded_reason="discount_below_threshold",
            )

        # --- Generar enlace de afiliado (modal Compartir) si aplica ---
        # Usar page.final_url (URL de artículo específico) en lugar de canonical_url
        # porque la URL /p/MLM... no muestra el botón Compartir
        affiliate = await self._maybe_extract_affiliate(product, fetch_url=page.final_url)

        if (
            self.affiliate_required_for_publish
            and (affiliate is None or not affiliate.affiliate_url)
        ):
            reason = "missing_affiliate_url"
            self._save_discard(
                url=product.canonical_url,
                reason=reason,
                payload={
                    "affiliate_error": (affiliate.error if affiliate else "no_extractor"),
                    "share_button": product.selected_variant_signals.get("share_button"),
                },
            )
            return MlHuntOutcome(
                url=url,
                final_url=page.final_url,
                extracted=product,
                classification=scoring.classification,
                suggested_outbox_type=outbox_type,
                enqueued_outbox_id=None,
                discarded_reason=reason,
                affiliate_url=None,
                affiliate_product_id=None,
                affiliate_error=(affiliate.error if affiliate else "no_extractor"),
            )

        offer_id = self._upsert_offer(product_id, scoring)
        self._update_product_affiliate(product_id, affiliate)
        outbox_id = self._enqueue_outbox(
            offer_id, product, scoring, outbox_type, affiliate
        )
        return MlHuntOutcome(
            url=url,
            final_url=page.final_url,
            extracted=product,
            classification=scoring.classification,
            suggested_outbox_type=outbox_type,
            enqueued_outbox_id=outbox_id,
            discarded_reason=None,
            affiliate_url=(affiliate.affiliate_url if affiliate else None),
            affiliate_product_id=(affiliate.affiliate_product_id if affiliate else None),
            affiliate_error=(affiliate.error if affiliate and not affiliate.success else None),
        )

    # ------------------------------------------------------------------
    # Affiliate
    # ------------------------------------------------------------------

    async def _maybe_extract_affiliate(
        self, product: ExtractedProduct, *, fetch_url: Optional[str] = None
    ) -> Optional[AffiliateInfo]:
        """Devuelve la info afiliada si hay extractor y share button presente.

        - Sin extractor configurado → None (caller decide qué hacer).
        - Sin share button visible → AffiliateInfo(error="no_share_button").
        - Caso normal → llama al extractor (que abre modal con Playwright).

        `fetch_url`: URL final de Playwright después de redirecciones. Se prefiere
        sobre `canonical_url` porque la URL /p/MLM... no muestra el botón Compartir
        pero la URL de artículo específico (articulo.mercadolibre.com.mx/MLM...) sí.
        """
        if self.affiliate_extractor is None:
            logger.debug("ml_hunter: no affiliate_extractor configured, skipping")
            return None
        if not product.selected_variant_signals.get("share_button"):
            return AffiliateInfo(success=False, error="no_share_button")
        # Preferir fetch_url (URL real visitada) sobre canonical_url
        url_to_use = fetch_url or product.canonical_url
        try:
            return await self.affiliate_extractor.extract(url_to_use)
        except Exception as exc:
            logger.warning("affiliate extractor raised: %s", exc)
            return AffiliateInfo(success=False, error=f"unexpected: {exc}")

    # ------------------------------------------------------------------
    # Decisión
    # ------------------------------------------------------------------

    def _decide_outbox_type(self, product: ExtractedProduct, scoring) -> Optional[str]:
        if scoring.classification in (
            Classification.PRICE_ERROR_CONFIRMED.value,
            Classification.POSSIBLE_PRICE_ERROR.value,
        ):
            return OutboxType.PRICE_ERROR.value
        if (
            product.discount_percent is not None
            and product.discount_percent >= self.normal_offer_min_discount
        ):
            return OutboxType.NORMAL.value
        return None

    def _build_signal(self, product: ExtractedProduct) -> PriceErrorSignal:
        return PriceErrorSignal(
            product_title=product.title or "(sin título)",
            marketplace=product.marketplace,
            source=Source.MERCADOLIBRE_HUNTER.value,
            original_url=product.url,
            resolved_url=product.canonical_url,
            current_price=product.current_price,
            previous_price=product.previous_price,
            discount_percent=product.discount_percent,
            brand=product.brand_guess,
            category=product.category_guess,
            condition=product.condition,
            has_stock=product.in_stock,
            has_image=bool(product.image_url),
            monthly_payment_suspected=product.is_monthly_payment,
            variant_mismatch=("variant_mismatch" in product.not_publishable_reasons),
            created_at=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # Persistencia
    # ------------------------------------------------------------------

    def _upsert_product(self, product: ExtractedProduct) -> int:
        if self.db is None:
            return 0
        existing = self.db.execute(
            "SELECT id FROM products WHERE url_canonical = ?", (product.canonical_url,)
        ).fetchone()
        if existing is not None:
            self.db.execute(
                "UPDATE products SET last_seen_at = ?, image_url = COALESCE(?, image_url), "
                "title = COALESCE(?, title), brand = COALESCE(?, brand), "
                "category = COALESCE(?, category), condition = ? WHERE id = ?",
                (
                    _now_iso(),
                    product.image_url,
                    product.title,
                    product.brand_guess,
                    product.category_guess,
                    product.condition,
                    existing["id"],
                ),
            )
            return existing["id"]
        cur = self.db.execute(
            "INSERT INTO products (marketplace, marketplace_id, url_canonical, title, brand, "
            "category, condition, image_url, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                product.marketplace,
                product.asin,  # MLM id en este caso
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

    def _save_price_observation(self, product_id: int, product: ExtractedProduct) -> None:
        if self.db is None or product_id == 0:
            return
        self.db.execute(
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
                Source.MERCADOLIBRE_HUNTER.value,
                json.dumps(
                    {
                        "raw_price_text": product.raw_price_text,
                        "raw_previous_price_text": product.raw_previous_price_text,
                        "extraction_warnings": product.extraction_warnings,
                        "extraction_confidence": product.extraction_confidence,
                        "share_button": product.selected_variant_signals.get(
                            "share_button"
                        ),
                    },
                    ensure_ascii=False,
                ),
                _now_iso(),
            ),
        )

    def _upsert_offer(self, product_id: int, scoring) -> int:
        if self.db is None:
            return 0
        cur = self.db.execute(
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

    def _enqueue_outbox(
        self,
        offer_id: int,
        product: ExtractedProduct,
        scoring,
        outbox_type: str,
        affiliate: Optional[AffiliateInfo] = None,
    ) -> Optional[int]:
        if self.db is None:
            return None

        # Gate: no re-publicar el mismo item_id (MLM...) en las últimas 48h
        item_id = product.asin  # en ML es el MLM id
        if item_id and self._recently_published(item_id, hours=48):
            logger.info(
                "ml_hunter: item %s publicado hace <48h, saltando", item_id
            )
            return None

        affiliate_url = affiliate.affiliate_url if affiliate else None
        affiliate_product_id = affiliate.affiliate_product_id if affiliate else None
        commission_text = affiliate.commission_text if affiliate else None

        # `url` se usa para publicación: si tenemos affiliate_url, lo preferimos.
        # `canonical_url` se conserva para revalidación / scraping.
        publish_url = affiliate_url or product.canonical_url

        payload = {
            "title": product.title,
            "current_price": product.current_price,
            "previous_price": product.previous_price,
            "discount_percent": product.discount_percent,
            "image_url": product.image_url,
            "url": publish_url,
            "canonical_url": product.canonical_url,
            "affiliate_url": affiliate_url,
            "affiliate_product_id": affiliate_product_id,
            "commission_text": commission_text,
            "marketplace": product.marketplace,
            "brand": product.brand_guess,
            "category": product.category_guess,
            "item_id": product.asin,
            "confidence_label": scoring.confidence_label,
            "score": scoring.score,
            "score_classification": scoring.classification,
            "in_stock": product.in_stock,
            "share_button": product.selected_variant_signals.get("share_button"),
            "source": Source.MERCADOLIBRE_HUNTER.value,
            "requires_live_validation": False,
        }
        cur = self.db.execute(
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

    def _update_product_affiliate(
        self, product_id: int, affiliate: Optional[AffiliateInfo]
    ) -> None:
        if self.db is None or product_id == 0 or affiliate is None:
            return
        if not (affiliate.affiliate_url or affiliate.affiliate_product_id):
            return
        self.db.execute(
            "UPDATE products SET affiliate_link = COALESCE(?, affiliate_link), "
            "affiliate_product_id = COALESCE(?, affiliate_product_id), "
            "commission_text = COALESCE(?, commission_text) WHERE id = ?",
            (
                affiliate.affiliate_url,
                affiliate.affiliate_product_id,
                affiliate.commission_text,
                product_id,
            ),
        )

    def _save_discard(self, *, url: str, reason: str, payload: dict) -> None:
        if self.db is None:
            return
        self.db.execute(
            "INSERT INTO discarded_candidates (source, raw_payload_json, reason, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                Source.MERCADOLIBRE_HUNTER.value,
                json.dumps({"url": url, **payload}, ensure_ascii=False),
                reason,
                _now_iso(),
            ),
        )

    def _recently_published(self, item_id: str, hours: int = 48) -> bool:
        """True si el item_id fue publicado exitosamente en las últimas `hours` horas."""
        if self.db is None:
            return False
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        row = self.db.execute(
            """
            SELECT pm.id FROM published_messages pm
            JOIN outbox o ON pm.outbox_id = o.id
            WHERE pm.success = 1
              AND pm.sent_at >= ?
              AND (
                json_extract(o.message_payload_json, '$.item_id') = ?
                OR json_extract(o.message_payload_json, '$.asin') = ?
              )
            LIMIT 1
            """,
            (cutoff, item_id, item_id),
        ).fetchone()
        return row is not None

    def _save_snapshot(self, url: str, page: RenderedPage, *, reason: str) -> Optional[int]:
        if self.db is None:
            return None
        cur = self.db.execute(
            "INSERT INTO dom_snapshots (marketplace, context, url, content, captured_at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "mercadolibre",
                "product_page",
                url,
                page.html or "",
                _now_iso(),
                reason,
            ),
        )
        return cur.lastrowid

    def _mark_visited(self, url: str) -> None:
        if self.db is None:
            return
        try:
            self.db.execute(
                "INSERT OR IGNORE INTO visited_urls (marketplace, url_canonical, visited_at) "
                "VALUES (?, ?, ?)",
                ("mercadolibre", url, _now_iso()),
            )
        except sqlite3.Error:
            pass

    def _emit_runtime_event(self, *, kind: str, severity: str, payload: dict) -> None:
        if self.db is None:
            return
        self.db.execute(
            "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (kind, severity, json.dumps(payload, ensure_ascii=False), _now_iso()),
        )


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
