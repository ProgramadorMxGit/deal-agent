"""Agente Amazon Hunter (versión inicial).

Lo justo para Fase 3.3:

- Lee seeds (URLs de producto Amazon) desde config o lista pasada por el
  caller.
- Para cada URL:
  - fetch HTML con `BrowserWorker`.
  - parsea con `AmazonProductParser`.
  - guarda `products` + `price_observation` en SQLite.
  - si `is_publishable` y discount/score cumplen reglas, encola en `outbox`.
  - si no, registra `discarded_candidates` con razón.
- Persiste `frontier`/`visited_urls` para futuros ciclos.

No hace exploración masiva (Fase 4). El objetivo es tener algo testeable y
funcional para validar páginas reales una por una y para alimentar el outbox
desde Amazon mismo, no sólo desde Telegram.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..browser.browser_context import BrowserWorker, RenderedPage
from ..extraction.amazon_product_parser import AmazonProductParser
from ..intelligence.price_error_scorer import PriceErrorScorer
from ..marketplaces.base import ExtractedProduct
from ..models import (
    Classification,
    OutboxState,
    OutboxType,
    PriceErrorSignal,
    Source,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resultado por URL
# ---------------------------------------------------------------------------


@dataclass
class HuntOutcome:
    url: str
    final_url: str
    extracted: Optional[ExtractedProduct]
    classification: str
    suggested_outbox_type: Optional[str]
    enqueued_outbox_id: Optional[int]
    discarded_reason: Optional[str]
    snapshot_id: Optional[int] = None
    # Captcha assessment del browser worker (`AmazonCaptchaDetector`).
    # Permite a los loops de orquestación distinguir captchas reales
    # high-confidence (deben pausar) de sospechas medium/low (no pausan).
    captcha_confidence: Optional[str] = None
    captcha_should_pause_marketplace: bool = False
    captcha_strong_signals: tuple[str, ...] = field(default_factory=tuple)
    captcha_visible_signals: tuple[str, ...] = field(default_factory=tuple)
    captcha_weak_signals: tuple[str, ...] = field(default_factory=tuple)
    captcha_debug_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Agente
# ---------------------------------------------------------------------------


class AmazonHunterAgent:
    """Hunter Amazon: dado URLs, extrae producto y crea offers/outbox."""

    def __init__(
        self,
        browser: BrowserWorker,
        *,
        db_conn: Optional[sqlite3.Connection] = None,
        parser: Optional[AmazonProductParser] = None,
        scorer: Optional[PriceErrorScorer] = None,
        normal_offer_min_discount: float = 50.0,
    ) -> None:
        self.browser = browser
        self.db = db_conn
        self.parser = parser or AmazonProductParser()
        self.scorer = scorer or PriceErrorScorer()
        self.normal_offer_min_discount = normal_offer_min_discount

    async def aclose(self) -> None:
        try:
            await self.browser.aclose()
        except Exception:
            pass

    async def hunt_urls(self, urls: list[str], *, max_urls: Optional[int] = None) -> list[HuntOutcome]:
        target = urls[: max_urls] if max_urls else urls
        outcomes: list[HuntOutcome] = []
        for url in target:
            outcome = await self.hunt_one(url)
            outcomes.append(outcome)
        return outcomes

    async def hunt_from_frontier(self, *, max_urls: int = 5) -> list[HuntOutcome]:
        """Toma URLs `kind=product` del frontier y las procesa.

        El frontier es donde el `DiscoveryAgent` deja productos descubiertos
        en listings/categorías. Esto convierte al hunter en un consumidor
        autónomo, no dependiente de seeds estáticas.
        """
        if self.db is None:
            return []
        from ..exploration.frontier import FrontierRepo

        frontier = FrontierRepo(self.db)
        items = frontier.pop("amazon", kind="product", limit=max_urls)
        if not items:
            return []
        outcomes: list[HuntOutcome] = []
        for item in items:
            try:
                outcome = await self.hunt_one(item.url)
            except Exception as exc:  # nunca rompemos el hunter
                logger.warning("hunt_one raised on %s: %s", item.url, exc)
                continue
            outcomes.append(outcome)
        return outcomes

    async def hunt_one(self, url: str) -> HuntOutcome:
        logger.info("amazon_hunter fetch %s", url)
        page = await self.browser.fetch(url)

        extras = page.extras or {}
        assessment_dict = extras.get("captcha_assessment") or {}
        confidence = assessment_dict.get("confidence")
        is_captcha = assessment_dict.get("is_captcha", False)
        should_pause = bool(assessment_dict.get("should_pause_marketplace", False))
        strong_signals = tuple(assessment_dict.get("strong_signals", []) or ())
        visible_signals = tuple(assessment_dict.get("visible_signals", []) or ())
        weak_signals = tuple(assessment_dict.get("weak_signals", []) or ())

        # Sospecha de captcha medium/low: el browser NO marcó `blocked` pero
        # hay estructura sospechosa. NO parseamos como producto, lo
        # tratamos como fetch fallido con razón clara.
        suspected_captcha = (
            is_captcha
            and confidence in ("medium", "low")
            and not page.blocked
        )

        if not page.ok or suspected_captcha:
            snapshot_id = self._save_snapshot(url, page, reason="fetch_failed")
            self._mark_visited(url)
            # Si el browser marcó `blocked=True` lo respetamos: en tests con
            # FakeBrowserWorker no hay assessment estructurado, y la
            # detección anterior (ML / legacy) ya tomó la decisión.
            blocked_by_browser = bool(page.blocked)

            debug_path: Optional[str] = None

            if blocked_by_browser and (confidence == "high" or not assessment_dict):
                # Captcha real confirmado — registrar evento y permitir
                # que el caller (orchestrator) decida la pausa de marketplace.
                self._emit_runtime_event(
                    kind="amazon_captcha_confirmed",
                    severity="error",
                    payload={
                        "url": url,
                        "final_url": page.final_url,
                        "status": page.status,
                        "strong_signals": list(strong_signals),
                        "visible_signals": list(visible_signals),
                        "should_pause_marketplace": should_pause,
                    },
                )
                reason_text = "captcha_detected"
                # Debug obligatorio (REGLA 6): guardamos snapshot HTML +
                # screenshot en data/debug/amazon_captcha/.
                debug_path = self._save_captcha_debug(url, page, assessment_dict)
            elif is_captcha and confidence in ("medium", "low"):
                # Sospecha de captcha SIN confianza alta → falso positivo
                # potencial. NO contar para pausa, NO marcar como captcha.
                ev_kind = (
                    "amazon_suspected_captcha_form_not_visible"
                    if (
                        "form_action_validate_captcha" in strong_signals
                        and not visible_signals
                    )
                    else "amazon_suspected_false_captcha"
                )
                self._emit_runtime_event(
                    kind=ev_kind,
                    severity="warning",
                    payload={
                        "url": url,
                        "final_url": page.final_url,
                        "status": page.status,
                        "confidence": confidence,
                        "strong_signals": list(strong_signals),
                        "weak_signals": list(weak_signals),
                        "visible_signals": list(visible_signals),
                        "reasons": assessment_dict.get("reasons", []),
                    },
                )
                reason_text = (
                    "amazon_possible_block_low_confidence"
                    if confidence == "low"
                    else "amazon_extraction_failed"
                )
                debug_path = self._save_captcha_debug(url, page, assessment_dict)
            else:
                reason_text = page.error or "fetch_failed"
            self._save_discard(
                url=url,
                reason=reason_text,
                payload={
                    "url": url,
                    "final_url": page.final_url,
                    "status": page.status,
                    "captcha_assessment": assessment_dict,
                },
            )
            return HuntOutcome(
                url=url,
                final_url=page.final_url,
                extracted=None,
                classification=("captcha" if reason_text == "captcha_detected" else "fetch_failed"),
                suggested_outbox_type=None,
                enqueued_outbox_id=None,
                discarded_reason=reason_text,
                snapshot_id=snapshot_id,
                captcha_confidence=confidence,
                captcha_should_pause_marketplace=should_pause and reason_text == "captcha_detected",
                captcha_strong_signals=strong_signals,
                captcha_visible_signals=visible_signals,
                captcha_weak_signals=weak_signals,
                captcha_debug_path=debug_path,
            )

        product = self.parser.parse(page.html, page.final_url)
        product_id = self._upsert_product(product)
        self._save_price_observation(product_id, product)
        self._mark_visited(product.canonical_url)

        # Reglas: si no es publicable, descartar.
        if not product.is_publishable:
            snapshot_id = self._save_snapshot(url, page, reason="not_publishable")
            reason = product.not_publishable_reasons[0] if product.not_publishable_reasons else "not_publishable"
            self._save_discard(
                url=product.canonical_url,
                reason=reason,
                payload={"reasons": product.not_publishable_reasons},
            )
            return HuntOutcome(
                url=url,
                final_url=page.final_url,
                extracted=product,
                classification="not_publishable",
                suggested_outbox_type=None,
                enqueued_outbox_id=None,
                discarded_reason=reason,
                snapshot_id=snapshot_id,
            )

        # Score con PriceErrorScorer
        signal = self._build_signal(product)
        scoring = self.scorer.score(signal)

        outbox_type = self._decide_outbox_type(product, scoring)
        if outbox_type is None:
            self._save_discard(
                url=product.canonical_url,
                reason="discount_below_threshold",
                payload={"score": scoring.score, "reasons": scoring.reasons},
            )
            return HuntOutcome(
                url=url,
                final_url=page.final_url,
                extracted=product,
                classification=scoring.classification,
                suggested_outbox_type=None,
                enqueued_outbox_id=None,
                discarded_reason="discount_below_threshold",
            )

        offer_id = self._upsert_offer(product_id, scoring)
        outbox_id = self._enqueue_outbox(offer_id, product, scoring, outbox_type)
        return HuntOutcome(
            url=url,
            final_url=page.final_url,
            extracted=product,
            classification=scoring.classification,
            suggested_outbox_type=outbox_type,
            enqueued_outbox_id=outbox_id,
            discarded_reason=None,
        )

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
            source=Source.AMAZON_HUNTER.value,
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
        cur = self.db.execute(
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

    def _save_price_observation(self, product_id: int, product: ExtractedProduct) -> Optional[int]:
        if self.db is None or product_id == 0:
            return None
        cur = self.db.execute(
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
                        "raw_price_text": product.raw_price_text,
                        "raw_previous_price_text": product.raw_previous_price_text,
                        "extraction_warnings": product.extraction_warnings,
                        "extraction_confidence": product.extraction_confidence,
                    },
                    ensure_ascii=False,
                ),
                _now_iso(),
            ),
        )
        return cur.lastrowid

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
                None,  # discount se llena en payload
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
    ) -> Optional[int]:
        if self.db is None:
            return None

        # Gate: no re-publicar el mismo ASIN en las últimas 48h
        if product.asin and self._recently_published(product.asin, hours=48):
            logger.info(
                "amazon_hunter: ASIN %s publicado hace <48h, saltando", product.asin
            )
            return None

        payload = {
            "title": product.title,
            "current_price": product.current_price,
            "previous_price": product.previous_price,
            "discount_percent": product.discount_percent,
            "image_url": product.image_url,
            "url": product.canonical_url,
            "marketplace": product.marketplace,
            "asin": product.asin,
            "confidence_label": scoring.confidence_label,
            "score": scoring.score,
            "score_classification": scoring.classification,
            "in_stock": product.in_stock,
            "source": Source.AMAZON_HUNTER.value,
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

    def _save_discard(self, *, url: str, reason: str, payload: dict) -> None:
        if self.db is None:
            return
        self.db.execute(
            "INSERT INTO discarded_candidates (source, raw_payload_json, reason, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                Source.AMAZON_HUNTER.value,
                json.dumps({"url": url, **payload}, ensure_ascii=False),
                reason,
                _now_iso(),
            ),
        )

    def _recently_published(self, asin: str, hours: int = 48) -> bool:
        """True si el ASIN fue publicado exitosamente en las últimas `hours` horas."""
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
              AND json_extract(o.message_payload_json, '$.asin') = ?
            LIMIT 1
            """,
            (cutoff, asin),
        ).fetchone()
        return row is not None

    def _save_snapshot(self, url: str, page: RenderedPage, *, reason: str) -> Optional[int]:
        if self.db is None:
            return None
        cur = self.db.execute(
            "INSERT INTO dom_snapshots (marketplace, context, url, content, captured_at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "amazon",
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
                ("amazon", url, _now_iso()),
            )
        except sqlite3.Error:
            pass

    def _emit_runtime_event(
        self, *, kind: str, severity: str, payload: dict
    ) -> None:
        if self.db is None:
            return
        try:
            self.db.execute(
                "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (kind, severity, json.dumps(payload, ensure_ascii=False), _now_iso()),
            )
        except sqlite3.Error as exc:
            logger.warning("runtime_events insert failed: %s", exc)

    def _save_captcha_debug(
        self,
        url: str,
        page: RenderedPage,
        assessment: dict,
    ) -> Optional[str]:
        """Guarda HTML + screenshot + metadatos en data/debug/amazon_captcha/.

        REGLA 6 del request del operador: cuando se detecta captcha (real o
        sospechoso), persistimos la evidencia en disco para auditoría
        manual. Esto sobrevive al reinicio y no depende de SQLite.
        """
        try:
            # Slug seguro: ASIN si está, si no la URL completa truncada.
            asin_match = re.search(r"/dp/([A-Z0-9]{10})", url)
            slug = asin_match.group(1) if asin_match else _slugify(url)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            base = Path("data") / "debug" / "amazon_captcha" / f"{ts}_{slug}"
            base.parent.mkdir(parents=True, exist_ok=True)
            html_path = base.with_suffix(".html")
            meta_path = base.with_suffix(".json")
            screenshot_path = base.with_suffix(".png")

            html_path.write_text(page.html or "", encoding="utf-8")
            meta = {
                "url": url,
                "final_url": page.final_url,
                "status": page.status,
                "duration_ms": page.duration_ms,
                "blocked": page.blocked,
                "error": page.error,
                "captcha_assessment": assessment,
                "captured_at": _now_iso(),
            }
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if page.screenshot_bytes:
                screenshot_path.write_bytes(page.screenshot_bytes)
            return str(base)
        except Exception as exc:  # pragma: no cover
            logger.warning("captcha debug save failed for %s: %s", url, exc)
            return None


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _slugify(value: str, *, max_len: int = 60) -> str:
    """Slug seguro para nombres de archivo (URL → guiones bajos)."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", value or "url")
    safe = safe.strip("_") or "url"
    return safe[:max_len]
