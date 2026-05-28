"""Tests del DiversityScorer (scoring determinístico de diversidad)."""
from datetime import datetime, timezone

import pytest

from ofertas_hunter.dispatching.diversity_scorer import (
    DiversityScorer,
    HistoryEntry,
    price_bucket,
)
from ofertas_hunter.models import OutboxItem, OutboxState, OutboxType


def _now():
    return datetime.now(timezone.utc)


def _item(
    *, id: int, marketplace: str = "amazon", category: str | None = None,
    brand: str | None = None, current_price: float = 1000.0,
    discount: float = 50.0,
) -> OutboxItem:
    return OutboxItem(
        id=id,
        offer_id=id,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": f"Item {id}",
            "marketplace": marketplace,
            "category": category,
            "brand": brand,
            "current_price": current_price,
            "discount_percent": discount,
        },
        enqueued_at=_now(),
        attempts=0,
        state=OutboxState.PENDING.value,
    )


def _hist(*, marketplace: str, category: str | None = None,
          brand: str | None = None, price_bucket: str = "mid") -> HistoryEntry:
    return HistoryEntry(
        marketplace=marketplace,
        category=category,
        brand=brand,
        price_bucket=price_bucket,
        sent_at=_now(),
    )


def test_price_bucket_categorizes_by_threshold():
    assert price_bucket(100) == "low"
    assert price_bucket(499) == "low"
    assert price_bucket(500) == "mid"
    assert price_bucket(2999) == "mid"
    assert price_bucket(3000) == "high"
    assert price_bucket(50000) == "high"


def test_scorer_returns_empty_when_no_candidates():
    scorer = DiversityScorer()
    result = scorer.rank([], history=[])
    assert result == []


def test_scorer_with_no_history_keeps_base_score():
    scorer = DiversityScorer()
    items = [_item(id=1, category="electronica"), _item(id=2, category="hogar")]
    result = scorer.rank(items, history=[])
    assert len(result) == 2
    # Sin historial, todos parten con score 1.0 (sin penalización ni bonus)
    assert all(c.score == pytest.approx(1.0) for c in result)



def test_scorer_penalizes_repeated_category():
    """Categoría que aparece 2 veces en historial recibe ×0.25."""
    scorer = DiversityScorer()
    items = [
        _item(id=1, category="mascotas"),
        _item(id=2, category="cocina"),
    ]
    history = [
        _hist(marketplace="ml", category="mascotas"),
        _hist(marketplace="ml", category="mascotas"),
    ]
    result = scorer.rank(items, history=history)
    by_id = {c.item.id: c for c in result}
    # mascotas tuvo 2 apariciones → 0.5^2 = 0.25
    # cocina ausente del historial → ×1.5
    assert by_id[1].score < by_id[2].score
    assert "category_penalty" in by_id[1].breakdown
    assert by_id[1].breakdown["category_penalty"] == pytest.approx(0.25)


def test_scorer_bonifica_categoria_ausente():
    scorer = DiversityScorer()
    items = [
        _item(id=1, category="electronica"),
        _item(id=2, category="hogar"),
    ]
    history = [_hist(marketplace="amazon", category="electronica")]
    result = scorer.rank(items, history=history)
    by_id = {c.item.id: c for c in result}
    # hogar no está en historial → bonus ×1.5
    assert by_id[2].breakdown.get("category_absent_bonus") == pytest.approx(1.5)
    assert by_id[2].score > by_id[1].score


def test_scorer_alterna_marketplace():
    scorer = DiversityScorer()
    items = [
        _item(id=1, marketplace="amazon", category="x"),
        _item(id=2, marketplace="mercadolibre", category="x"),
    ]
    history = [_hist(marketplace="amazon", category="otro")]
    result = scorer.rank(items, history=history)
    by_id = {c.item.id: c for c in result}
    # ml no fue el último, sin penalización
    # amazon fue el último → ×0.7
    assert by_id[1].breakdown.get("marketplace_alternation") == pytest.approx(0.7)
    assert by_id[2].score > by_id[1].score


def test_scorer_top_n_limita_resultado():
    scorer = DiversityScorer(top_n=3)
    items = [_item(id=i, category=f"cat{i}") for i in range(10)]
    result = scorer.rank(items, history=[])
    assert len(result) == 3


def test_scorer_orden_estable_cuando_scores_iguales():
    """Items con mismo score mantienen orden de entrada (sort estable)."""
    scorer = DiversityScorer()
    items = [_item(id=1), _item(id=2), _item(id=3)]
    result = scorer.rank(items, history=[])
    assert [c.item.id for c in result] == [1, 2, 3]
