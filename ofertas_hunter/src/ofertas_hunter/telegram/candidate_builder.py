"""Convierte un mensaje Telegram parseado en un candidato para `price_intelligence`.

Decisión de clasificación interna (independiente del scorer numérico):

- `ignored_mercadolibre_from_telegram`: link era ML (resuelto o no).
- `noise`: sin link válido, sin precio extraíble y sin señales de urgencia.
- `telegram_price_error_signal`: tiene señales fuertes de error de precio
  (keyword "ERROR DE PRECIO" o urgencia alta).
- `telegram_deal_signal`: parece oferta normal con descuento visible o con
  formato Amazon estructurado que requiere revalidación live.

Para los candidatos no descartados, se llama al `PriceErrorScorer` y se
construye opcionalmente un `OutboxItem` en estado `pending_revalidation`
(ver `RevalidationGate`). **Nunca** se publica directamente desde Telegram.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..intelligence.price_error_scorer import PriceErrorScorer
from ..models import (
    Classification,
    OutboxItem,
    OutboxState,
    OutboxType,
    PriceErrorSignal,
    ScoringResult,
    Source,
)
from .link_resolver import ResolvedLink, is_mercadolibre_url
from .message_parser import ParsedTelegramMessage


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------


# Clasificación interna del listener (no es la del scorer)
LISTENER_PRICE_ERROR = "telegram_price_error_signal"
LISTENER_DEAL = "telegram_deal_signal"
LISTENER_IGNORED_ML = "ignored_mercadolibre_from_telegram"
LISTENER_NOISE = "noise"


@dataclass
class TelegramCandidate:
    """Candidato listo para entregarse a price_intelligence/outbox.

    `internal_classification` es la decisión rápida del listener (cualitativa,
    independiente del scorer numérico). `signal` y `scoring` son el output del
    PriceErrorScorer cuando aplica.

    Si `outbox_item` no es None, está listo para encolarse como
    `pending_revalidation` (no se publica hasta que un Playwright revalidator
    confirme producto, precio, imagen, stock).
    """

    parsed: ParsedTelegramMessage
    internal_classification: str
    reasons: list[str] = field(default_factory=list)
    signal: Optional[PriceErrorSignal] = None
    scoring: Optional[ScoringResult] = None
    outbox_item: Optional[OutboxItem] = None
    requires_live_validation: bool = True

    @property
    def is_actionable(self) -> bool:
        return self.internal_classification in (LISTENER_PRICE_ERROR, LISTENER_DEAL)

    @property
    def is_ignored(self) -> bool:
        return self.internal_classification in (LISTENER_IGNORED_ML, LISTENER_NOISE)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class TelegramCandidateBuilder:
    """Convierte ParsedTelegramMessage + ResolvedLink en TelegramCandidate."""

    def __init__(
        self,
        scorer: Optional[PriceErrorScorer] = None,
        *,
        ignore_mercadolibre_links: bool = True,
        normal_offer_min_discount: float = 50.0,
    ) -> None:
        self.scorer = scorer or PriceErrorScorer()
        self.ignore_mercadolibre = ignore_mercadolibre_links
        self.normal_offer_min_discount = normal_offer_min_discount

    def build(
        self,
        parsed: ParsedTelegramMessage,
        resolved: Optional[ResolvedLink] = None,
    ) -> TelegramCandidate:
        """Aplica las reglas y devuelve el TelegramCandidate.

        El builder **no** persiste a SQLite — eso lo hace el agente listener,
        que tiene la conexión.
        """
        # --- Gate 1: Mercado Libre desde Telegram ---
        if self._is_mercadolibre_telegram(parsed, resolved):
            return TelegramCandidate(
                parsed=parsed,
                internal_classification=LISTENER_IGNORED_ML,
                reasons=["mercadolibre_link_from_telegram"],
            )

        # --- Gate 2: noise (sin link Y sin señal útil) ---
        if self._is_noise(parsed, resolved):
            return TelegramCandidate(
                parsed=parsed,
                internal_classification=LISTENER_NOISE,
                reasons=["no_link_no_price_no_urgency"],
            )

        # --- Construir signal para el scorer ---
        signal = self._build_signal(parsed, resolved)
        scoring = self.scorer.score(signal)

        # --- Decidir clasificación interna ---
        # Reglas de prioridad (de mayor a menor):
        # 1. Texto explícito de "ERROR DE PRECIO" o urgencia alta → PE.
        # 2. Descuento visible >= 80% (extremo) → PE aunque venga sin urgencia.
        # 3. Score del scorer >= confirmed/possible → PE.
        # 4. Score 40-59 (suspicious) → PE como possible_pe.
        # 5. Discount visible >= normal_offer_min_discount → DEAL.
        # 6. Amazon con formato estructurado de cupón/precio final → DEAL
        #    pendiente de revalidación live.
        # 7. Resto → noise.
        score_label = scoring.classification
        is_strong_pe = (
            parsed.is_price_error_keyword
            or parsed.urgency_score >= 25
            or score_label
            in (
                Classification.PRICE_ERROR_CONFIRMED.value,
                Classification.POSSIBLE_PRICE_ERROR.value,
            )
            or (parsed.discount_visible is not None and parsed.discount_visible >= 80.0)
        )

        if is_strong_pe:
            internal = LISTENER_PRICE_ERROR
            outbox_type = OutboxType.PRICE_ERROR.value
        elif scoring.score >= 40:
            internal = LISTENER_PRICE_ERROR  # suspicious_deal entra como PE
            outbox_type = OutboxType.POSSIBLE_PE.value
        elif (
            parsed.discount_visible is not None
            and parsed.discount_visible >= self.normal_offer_min_discount
        ):
            internal = LISTENER_DEAL
            outbox_type = OutboxType.NORMAL.value
        elif self._is_amazon_structured_offer(parsed):
            internal = LISTENER_DEAL
            outbox_type = OutboxType.NORMAL.value
        else:
            return TelegramCandidate(
                parsed=parsed,
                internal_classification=LISTENER_NOISE,
                reasons=[
                    "score_below_threshold",
                    f"score={scoring.score}",
                ],
                signal=signal,
                scoring=scoring,
            )

        # Sólo creamos OutboxItem en pending_revalidation si tenemos los
        # mínimos para una eventual publicación tras revalidación Playwright.
        outbox_item: Optional[OutboxItem] = None
        if self._can_enqueue_pending(parsed, resolved):
            outbox_item = self._build_outbox_item(
                parsed=parsed,
                resolved=resolved,
                outbox_type=outbox_type,
                signal=signal,
                scoring=scoring,
            )

        reasons = list(scoring.reasons)
        reasons.append(f"telegram_internal={internal}")
        if parsed.urgency_terms:
            reasons.append("urgency=" + ",".join(parsed.urgency_terms[:5]))

        return TelegramCandidate(
            parsed=parsed,
            internal_classification=internal,
            reasons=reasons,
            signal=signal,
            scoring=scoring,
            outbox_item=outbox_item,
            requires_live_validation=True,
        )

    # ------------------------------------------------------------------
    # Reglas
    # ------------------------------------------------------------------

    def _is_mercadolibre_telegram(
        self,
        parsed: ParsedTelegramMessage,
        resolved: Optional[ResolvedLink],
    ) -> bool:
        if not self.ignore_mercadolibre:
            return False
        if parsed.skip_reason == "mercadolibre_link":
            return True
        if parsed.marketplace == "mercadolibre":
            return True
        if resolved and resolved.is_mercadolibre:
            return True
        return False

    def _is_noise(
        self,
        parsed: ParsedTelegramMessage,
        resolved: Optional[ResolvedLink],
    ) -> bool:
        no_link = parsed.original_url is None and (resolved is None or not resolved.final_url)
        no_price = parsed.written_price is None
        no_urgency = parsed.urgency_score == 0 and not parsed.is_price_error_keyword
        no_discount = parsed.discount_visible is None
        return no_link and no_price and no_urgency and no_discount

    def _is_amazon_structured_offer(self, parsed: ParsedTelegramMessage) -> bool:
        if parsed.marketplace != "amazon":
            return False
        if parsed.written_price is None or not parsed.original_url:
            return False

        text = parsed.text.lower()
        return (
            "precio oferta +" in text
            or "precio oferta:" in text
            or re.search(r"\bde\s*\$?\s*[0-9][0-9\.,]*\s*a\s*\$?\s*[0-9][0-9\.,]*\b", text)
            is not None
        )

    def _can_enqueue_pending(
        self,
        parsed: ParsedTelegramMessage,
        resolved: Optional[ResolvedLink],
    ) -> bool:
        # Sin link no podemos publicar (RULES.md §6).
        url = (resolved.best_url() if resolved else None) or parsed.original_url
        if not url:
            return False
        # Sin precio extraíble del mensaje, no podemos formatear.
        # El revalidator real lo confirmará en página, pero ya lo necesitamos
        # como hint en el payload.
        if parsed.written_price is None:
            return False
        return True

    # ------------------------------------------------------------------
    # Constructores
    # ------------------------------------------------------------------

    def _build_signal(
        self,
        parsed: ParsedTelegramMessage,
        resolved: Optional[ResolvedLink],
    ) -> PriceErrorSignal:
        return PriceErrorSignal(
            product_title=parsed.title_guess or "(sin título)",
            marketplace=parsed.marketplace or (parsed.store_mention or "other"),
            source=Source.TELEGRAM.value,
            original_url=parsed.original_url or "",
            source_channel=parsed.channel,
            original_text=parsed.text,
            resolved_url=(resolved.final_url if resolved else None),
            current_price=parsed.written_price,
            previous_price=None,
            discount_percent=parsed.discount_visible,
            brand=parsed.brand,
            category=parsed.category,
            has_stock=None,
            has_image=parsed.has_image,
            urgency_terms=list(parsed.urgency_terms),
            telegram_confidence=float(parsed.urgency_score),
            created_at=parsed.captured_at,
        )

    def _build_outbox_item(
        self,
        *,
        parsed: ParsedTelegramMessage,
        resolved: Optional[ResolvedLink],
        outbox_type: str,
        signal: PriceErrorSignal,
        scoring: ScoringResult,
    ) -> OutboxItem:
        url = (resolved.best_url() if resolved else None) or parsed.original_url or ""

        payload = {
            "title": parsed.title_guess or "(sin título)",
            "current_price": parsed.written_price,
            "url": url,
            "image_url": parsed.image_path,
            "marketplace": parsed.marketplace or "other",
            "confidence_label": scoring.confidence_label,
            "discount_percent": parsed.discount_visible,
            # Marcadores duros para que el dispatcher entienda que esto NO se
            # publica sin revalidación Playwright.
            "requires_live_validation": True,
            "source": Source.TELEGRAM.value,
            "source_channel": parsed.channel,
            "original_url": parsed.original_url,
            "resolved_url": (resolved.final_url if resolved else None),
            "urgency_terms": list(parsed.urgency_terms),
            "score": scoring.score,
            "score_classification": scoring.classification,
            "internal_classification": LISTENER_PRICE_ERROR
            if outbox_type != OutboxType.NORMAL.value
            else LISTENER_DEAL,
        }

        return OutboxItem(
            offer_id=0,  # el agent lo rellenará al persistir el offer real
            type=outbox_type,
            message_payload=payload,
            state=OutboxState.PENDING.value,
            enqueued_at=datetime.now(timezone.utc),
        )
