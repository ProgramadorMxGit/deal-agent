"""Scoring determinístico de diversidad para candidatos del outbox.

Asigna a cada candidato un score que penaliza repetición de
categoría / marca / marketplace / rango de precio respecto al historial
reciente, y bonifica categorías ausentes.

Output: lista ordenada de mayor a menor score, top N candidatos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..models import OutboxItem


PRICE_BUCKET_LOW_MAX = 500.0
PRICE_BUCKET_MID_MAX = 3000.0


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

    Reglas (multiplicadores sumativos sobre base 1.0):
    - Misma categoría aparece N veces en historial: × 0.5^N
    - Misma marca aparece N veces: × 0.7^N
    - Mismo marketplace que el último: × 0.7
    - Mismo bucket de precio que el último: × 0.85
    - Categoría AUSENTE en historial: × 1.5
    """

    def __init__(self, *, history_size: int = 10, top_n: int = 10) -> None:
        self.history_size = history_size
        self.top_n = top_n

    def rank(
        self,
        candidates: list[OutboxItem],
        history: list[HistoryEntry],
    ) -> list[ScoredCandidate]:
        if not candidates:
            return []
        scored: list[ScoredCandidate] = []
        for item in candidates:
            score, breakdown = self._score_item(item, history)
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
        bd: dict[str, float] = {}

        if history:
            # Penalización por categoría
            if cat is not None:
                cat_count = sum(1 for h in history if h.category == cat)
                if cat_count > 0:
                    factor = 0.5 ** cat_count
                    score *= factor
                    bd["category_penalty"] = factor
            # Penalización por marca
            if brand is not None:
                brand_count = sum(1 for h in history if h.brand == brand)
                if brand_count > 0:
                    factor = 0.7 ** brand_count
                    score *= factor
                    bd["brand_penalty"] = factor
            # Penalización por marketplace si fue el último
            last = history[0]
            if last.marketplace == marketplace:
                score *= 0.7
                bd["marketplace_alternation"] = 0.7
            # Penalización por bucket de precio si fue el último
            if last.price_bucket == bucket:
                score *= 0.85
                bd["price_bucket_alternation"] = 0.85
            # Bonus por categoría ausente
            if cat is not None:
                seen_categories = {h.category for h in history if h.category}
                if cat not in seen_categories:
                    score *= 1.5
                    bd["category_absent_bonus"] = 1.5

        return score, bd
