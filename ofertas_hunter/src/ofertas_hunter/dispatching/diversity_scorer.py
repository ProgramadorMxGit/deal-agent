"""Scoring determinístico de diversidad para candidatos del outbox.

Asigna a cada candidato un score que penaliza repetición de
categoría / marca / marketplace / rango de precio respecto al historial
reciente, y bonifica categorías ausentes.

Output: lista ordenada de mayor a menor score, top N candidatos.

Convención de historial:
    `history[0]` representa la publicación MÁS RECIENTE.
    `history[-1]` la más antigua dentro de la ventana retenida.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..models import OutboxItem


# Umbrales de bucket de precio (MXN)
PRICE_BUCKET_LOW_MAX = 500.0
PRICE_BUCKET_MID_MAX = 3000.0

# Multiplicadores de scoring (centralizados para tunear sin tocar lógica)
CATEGORY_REPEAT_BASE = 0.5         # × CATEGORY_REPEAT_BASE^N por N apariciones de la categoría
BRAND_REPEAT_BASE = 0.7            # × BRAND_REPEAT_BASE^N por N apariciones de la marca
LAST_MARKETPLACE_PENALTY = 0.7     # × penalización si coincide marketplace con la última publicación
LAST_PRICE_BUCKET_PENALTY = 0.85   # × penalización si coincide bucket de precio con la última
CATEGORY_ABSENT_BONUS = 1.5        # × bonus si la categoría no aparece en historial


def price_bucket(price: Optional[float]) -> str:
    """Clasifica un precio en low / mid / high.

    - low:  < 500 MXN
    - mid:  [500, 3000)
    - high: >= 3000
    """
    if price is None:
        return "mid"
    p = float(price)
    if p < PRICE_BUCKET_LOW_MAX:
        return "low"
    if p < PRICE_BUCKET_MID_MAX:
        return "mid"
    return "high"


@dataclass(frozen=True)
class HistoryEntry:
    """Una entrada del historial reciente de publicaciones.

    El consumer (DiversityCurator) construye una lista de ``HistoryEntry`` ordenada
    de **más reciente a más antiguo** (``history[0]`` = última publicación enviada).
    """

    marketplace: str
    category: Optional[str]
    brand: Optional[str]
    price_bucket: str
    sent_at: datetime


@dataclass
class ScoredCandidate:
    item: OutboxItem
    score: float
    breakdown: dict[str, float] = field(default_factory=dict)


class DiversityScorer:
    """Calcula score de diversidad para candidatos.

    Reglas (multiplicadores aplicados sobre base 1.0):
    - Misma categoría aparece N veces en historial: × ``CATEGORY_REPEAT_BASE^N``
    - Misma marca aparece N veces: × ``BRAND_REPEAT_BASE^N``
    - Mismo marketplace que el último: × ``LAST_MARKETPLACE_PENALTY``
    - Mismo bucket de precio que el último: × ``LAST_PRICE_BUCKET_PENALTY``
    - Categoría AUSENTE en historial: × ``CATEGORY_ABSENT_BONUS``
    """

    def __init__(self, *, history_size: int = 10, top_n: int = 10) -> None:
        self.history_size = history_size
        self.top_n = top_n

    def rank(
        self,
        candidates: list[OutboxItem],
        history: list[HistoryEntry],
    ) -> list[ScoredCandidate]:
        """Puntúa y ordena ``candidates`` según diversidad respecto al ``history``.

        ``history`` debe estar ordenada **de más reciente a más antigua**
        (``history[0]`` = última publicación). Sólo se consideran las primeras
        ``self.history_size`` entradas; el resto se ignora.

        Retorna una lista de ``ScoredCandidate`` ordenada de mayor a menor score,
        truncada a ``self.top_n``.
        """
        if not candidates:
            return []
        # Honrar la ventana de historial declarada en el constructor.
        effective_history = history[: self.history_size] if history else []
        scored: list[ScoredCandidate] = []
        for item in candidates:
            score, breakdown = self._score_item(item, effective_history)
            scored.append(ScoredCandidate(item=item, score=score, breakdown=breakdown))
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[: self.top_n]

    def _score_item(
        self,
        item: OutboxItem,
        history: list[HistoryEntry],
    ) -> tuple[float, dict[str, float]]:
        payload = item.message_payload or {}
        cat = payload.get("category")
        brand = payload.get("brand")
        marketplace = payload.get("marketplace") or "unknown"
        bucket = price_bucket(payload.get("current_price"))

        score = 1.0
        breakdown: dict[str, float] = {}

        if history:
            # Penalización por categoría
            if cat is not None:
                cat_count = sum(1 for h in history if h.category == cat)
                if cat_count > 0:
                    factor = CATEGORY_REPEAT_BASE ** cat_count
                    score *= factor
                    breakdown["category_penalty"] = factor
            # Penalización por marca
            if brand is not None:
                brand_count = sum(1 for h in history if h.brand == brand)
                if brand_count > 0:
                    factor = BRAND_REPEAT_BASE ** brand_count
                    score *= factor
                    breakdown["brand_penalty"] = factor
            # Penalización por marketplace si fue el último
            last = history[0]
            if last.marketplace == marketplace:
                score *= LAST_MARKETPLACE_PENALTY
                breakdown["marketplace_alternation"] = LAST_MARKETPLACE_PENALTY
            # Penalización por bucket de precio si fue el último
            if last.price_bucket == bucket:
                score *= LAST_PRICE_BUCKET_PENALTY
                breakdown["price_bucket_alternation"] = LAST_PRICE_BUCKET_PENALTY
            # Bonus por categoría ausente
            if cat is not None:
                seen_categories = {h.category for h in history if h.category}
                if cat not in seen_categories:
                    score *= CATEGORY_ABSENT_BONUS
                    breakdown["category_absent_bonus"] = CATEGORY_ABSENT_BONUS

        return score, breakdown
