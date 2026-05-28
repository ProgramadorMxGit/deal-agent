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


def _hist(
    *,
    marketplace: str,
    category: str | None = None,
    brand: str | None = None,
    bucket: str = "mid",
) -> HistoryEntry:
    """Construye una HistoryEntry. ``bucket`` evita shadowear ``price_bucket``."""
    return HistoryEntry(
        marketplace=marketplace,
        category=category,
        brand=brand,
        price_bucket=bucket,
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


def test_scorer_brand_penalty_path():
    """Misma marca repetida 2 veces → ×0.7^2 = 0.49 sobre el item con esa marca."""
    scorer = DiversityScorer()
    items = [
        _item(id=1, category="x", brand="logitech"),
        _item(id=2, category="x", brand="razer"),
    ]
    history = [
        _hist(marketplace="ml", category="otra", brand="logitech"),
        _hist(marketplace="ml", category="otra", brand="logitech"),
    ]
    result = scorer.rank(items, history=history)
    by_id = {c.item.id: c for c in result}
    assert by_id[1].breakdown.get("brand_penalty") == pytest.approx(0.49)
    assert "brand_penalty" not in by_id[2].breakdown
    assert by_id[2].score > by_id[1].score


def test_scorer_price_bucket_alternation_path():
    """Mismo bucket de precio que la última publicación → ×0.85."""
    scorer = DiversityScorer()
    # current_price 1000 → bucket 'mid'; 100 → bucket 'low'
    items = [
        _item(id=1, category="x", current_price=1000.0),  # mid
        _item(id=2, category="x", current_price=100.0),   # low
    ]
    # último publicado fue mid → item 1 cae en alternación
    history = [_hist(marketplace="otro", category="otra", bucket="mid")]
    result = scorer.rank(items, history=history)
    by_id = {c.item.id: c for c in result}
    assert by_id[1].breakdown.get("price_bucket_alternation") == pytest.approx(0.85)
    assert "price_bucket_alternation" not in by_id[2].breakdown
    assert by_id[2].score > by_id[1].score


def test_scorer_combined_multipliers():
    """Item que coincide en categoría + marca + marketplace + bucket recibe el producto de penalizaciones.

    Histórico: 1 entrada con misma categoría, misma marca, mismo marketplace, mismo bucket.
    Multiplicadores esperados:
        category_penalty       = 0.5^1  = 0.5
        brand_penalty          = 0.7^1  = 0.7
        marketplace_alternation= 0.7
        price_bucket_alternation=0.85
    Producto = 0.5 * 0.7 * 0.7 * 0.85 = 0.20825
    """
    scorer = DiversityScorer()
    items = [
        _item(
            id=1,
            marketplace="amazon",
            category="hogar",
            brand="acme",
            current_price=1000.0,  # bucket mid
        ),
    ]
    history = [
        _hist(marketplace="amazon", category="hogar", brand="acme", bucket="mid"),
    ]
    result = scorer.rank(items, history=history)
    assert len(result) == 1
    sc = result[0]
    assert sc.breakdown["category_penalty"] == pytest.approx(0.5)
    assert sc.breakdown["brand_penalty"] == pytest.approx(0.7)
    assert sc.breakdown["marketplace_alternation"] == pytest.approx(0.7)
    assert sc.breakdown["price_bucket_alternation"] == pytest.approx(0.85)
    # Asegurar que NO se aplicó el bonus por categoría ausente (la categoría sí estaba en historial)
    assert "category_absent_bonus" not in sc.breakdown
    expected = 0.5 * 0.7 * 0.7 * 0.85
    assert sc.score == pytest.approx(expected)


def test_scorer_history_size_truncates_window():
    """``history_size`` debe limitar la ventana de historial considerada.

    Con ``history_size=2`` y un historial con 5 entradas de la misma categoría,
    sólo deben contar las 2 más recientes → 0.5^2 = 0.25 (no 0.5^5 = 0.03125).
    """
    scorer = DiversityScorer(history_size=2)
    items = [_item(id=1, category="repetida")]
    history = [_hist(marketplace="ml", category="repetida") for _ in range(5)]
    result = scorer.rank(items, history=history)
    assert len(result) == 1
    assert result[0].breakdown["category_penalty"] == pytest.approx(0.25)
