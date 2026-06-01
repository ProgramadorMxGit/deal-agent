"""Tests de topes duros de diversidad (hard caps).

Cubre criterios del spec:
F. ventana con 3 proteínas bloquea 4ª proteína si hay alternativa.
G. ventana con 2 BHP bloquea 3ª BHP si hay alternativa.
H. fuzzy title >= 85% bloquea duplicado.
(familia) variante del mismo producto bloqueada en ventana/24h.
(least_repetitive) elige el menos malo para override.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ofertas_hunter.dispatching.diversity_filters import (
    CandidateMeta,
    HardCapConfig,
    HistoryItem,
    REASON_BRAND_WINDOW,
    REASON_CATEGORY_WINDOW,
    REASON_FAMILY_WINDOW,
    REASON_FUZZY_TITLE,
    apply_hard_caps,
    least_repetitive,
)
from ofertas_hunter.models import OutboxItem, OutboxState, OutboxType


def _now():
    return datetime(2026, 5, 30, 22, 0, 0, tzinfo=timezone.utc)


def _item(id: int) -> OutboxItem:
    return OutboxItem(
        id=id, offer_id=id, type=OutboxType.NORMAL.value,
        message_payload={"title": f"t{id}"}, enqueued_at=_now(),
        state=OutboxState.PENDING.value,
    )


def _meta(id, *, cat=None, brand=None, fam=None, mkt="mercadolibre", title="t", item_id=None):
    return CandidateMeta(
        item=_item(id), marketplace=mkt, category=cat, brand=brand,
        product_family=fam, title_fingerprint=title, title=title, item_id=item_id,
    )


def _hist(*, cat=None, brand=None, fam=None, mkt="mercadolibre", title="t", item_id=None, ago_h=0.1):
    return HistoryItem(
        marketplace=mkt, category=cat, brand=brand, product_family=fam,
        title_fingerprint=title, item_id=item_id,
        sent_at=_now() - timedelta(hours=ago_h),
    )


def _cfg(**kw):
    base = dict(
        enabled=True, window_size=10, max_same_category=3, max_same_brand=2,
        max_same_product_family=1, max_same_marketplace=7,
        fuzzy_title_threshold=0.85, reject_similar_hours=24,
        allow_override_if_no_alternative=True,
    )
    base.update(kw)
    return HardCapConfig(**base)


# F — 3 proteínas en ventana bloquea la 4ª si hay alternativa
def test_F_three_protein_blocks_fourth():
    history = [
        _hist(cat="proteina/suplementos", fam=f"fam{i}", brand=f"b{i}",
              title=f"proteina marca{i} whey {i} kilos") for i in range(3)
    ]
    candidates = [
        _meta(1, cat="proteina/suplementos", fam="fp", brand="bp",
              title="proteina cuarta marca whey distinta"),
        _meta(2, cat="tecnologia", fam="ft", brand="bt",
              title="laptop hp core i5 ssd"),
    ]
    res = apply_hard_caps(candidates, history, _cfg(), _now())
    kept_ids = {c.item.id for c in res.kept}
    assert 2 in kept_ids  # tecnología pasa
    assert 1 not in kept_ids  # 4ª proteína bloqueada
    reasons = {r.outbox_id: r.reason for r in res.rejected}
    assert reasons.get(1) == REASON_CATEGORY_WINDOW


# G — 2 BHP en ventana bloquea la 3ª si hay alternativa
def test_G_two_bhp_blocks_third():
    history = [
        _hist(cat="proteina/suplementos", brand="bhp", fam="f1", title="bhp whey vainilla uno"),
        _hist(cat="proteina/suplementos", brand="bhp", fam="f2", title="bhp iso protein ponche dos"),
    ]
    candidates = [
        _meta(1, cat="proteina/suplementos", brand="bhp", fam="f3",
              title="bhp just whey natural tres"),  # 3ª BHP
        _meta(2, cat="proteina/suplementos", brand="empower", fam="f4",
              title="empower gold whey cuatro"),
    ]
    res = apply_hard_caps(candidates, history, _cfg(max_same_category=5), _now())
    kept_ids = {c.item.id for c in res.kept}
    assert 1 not in kept_ids
    assert 2 in kept_ids
    reasons = {r.outbox_id: r.reason for r in res.rejected}
    assert reasons.get(1) == REASON_BRAND_WINDOW


# H — fuzzy title >= 85% bloquea
def test_H_fuzzy_title_blocks():
    history = [_hist(
        cat="proteina/suplementos", brand="bhp", fam="famX",
        title="proteina bhp ultra whey ultra kg lbs bote",
    )]
    candidates = [
        _meta(1, cat="proteina/suplementos", brand="bhp", fam="famY",
              title="proteina bhp ultra whey ultra kg lbs bote"),
    ]
    res = apply_hard_caps(candidates, history, _cfg(max_same_category=9, max_same_brand=9), _now())
    assert not res.kept
    assert res.rejected[0].reason in (REASON_FUZZY_TITLE, REASON_FAMILY_WINDOW)


# familia — misma familia en ventana bloquea segunda
def test_family_window_blocks_second_variant():
    history = [_hist(cat="proteina/suplementos", brand="bhp", fam="bhp_ultra_whey", title="a")]
    candidates = [
        _meta(1, cat="proteina/suplementos", brand="bhp", fam="bhp_ultra_whey", title="zzz different"),
        _meta(2, cat="tecnologia", brand="hp", fam="hp_laptop", title="qqq"),
    ]
    res = apply_hard_caps(candidates, history, _cfg(max_same_category=9, max_same_brand=9), _now())
    kept_ids = {c.item.id for c in res.kept}
    assert 1 not in kept_ids
    assert 2 in kept_ids


# disabled → no filtra
def test_disabled_keeps_all():
    history = [_hist(cat="proteina/suplementos", brand="bhp", fam="f") for _ in range(5)]
    candidates = [_meta(1, cat="proteina/suplementos", brand="bhp", fam="f")]
    res = apply_hard_caps(candidates, history, _cfg(enabled=False), _now())
    assert len(res.kept) == 1


# marketplace cap solo aplica si hay alternativa de otro mkt
def test_marketplace_cap_needs_alternative():
    history = [
        _hist(mkt="mercadolibre", cat=f"c{i}", brand=f"b{i}", fam=f"f{i}",
              title=f"producto categoria {i} distinto")
        for i in range(7)
    ]
    # solo candidatos ML → no se aplica cap de marketplace (no hay alternativa)
    candidates = [_meta(1, cat="x", brand="bx", fam="fx", mkt="mercadolibre",
                        title="producto unico sin repetir nada")]
    res = apply_hard_caps(candidates, history, _cfg(), _now())
    assert len(res.kept) == 1  # no bloqueado porque no hay alternativa de otro mkt


# least_repetitive elige el menos malo
def test_least_repetitive_picks_least_seen():
    history = [
        _hist(cat="proteina/suplementos", brand="bhp", fam="bhp_whey"),
        _hist(cat="proteina/suplementos", brand="bhp", fam="bhp_whey"),
        _hist(cat="proteina/suplementos", brand="43 supplements", fam="43_zero"),
    ]
    candidates = [
        _meta(1, cat="proteina/suplementos", brand="bhp", fam="bhp_whey"),       # muy repetido
        _meta(2, cat="proteina/suplementos", brand="empower", fam="empower_whey"),  # menos repetido
    ]
    chosen = least_repetitive(candidates, history, _cfg())
    assert chosen.item.id == 2
