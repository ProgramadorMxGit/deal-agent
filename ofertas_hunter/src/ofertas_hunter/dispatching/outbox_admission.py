"""Admisión al pending del outbox con cuotas por categoría/marca/marketplace.

Objetivo (Opción A): mantener `NORMAL_OFFER_MIN_DISCOUNT=50` global y NO pausar
el dispatch, pero evitar que una sola categoría/marca/marketplace sature el
pool publicable (`pending`). Los items que saturarían una cuota se encolan como
`deferred` (NO descartados) con `scheduled_for` futuro y metadata de
trazabilidad; un job posterior los promueve a `pending`.

NO toca gates de seguridad. Un item que llega aquí YA pasó descuento>=50 y todos
los gates del publisher/hunter. Esto solo decide pending vs deferred.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..dispatching.diversity_metadata import (
    compute_diversity_metadata,
    infer_offer_category,
    normalize_brand,
)

logger = logging.getLogger(__name__)


DEFER_REASON_CATEGORY = "category_quota_saturated"
DEFER_REASON_BRAND = "brand_quota_saturated"
DEFER_REASON_MARKETPLACE = "marketplace_quota_saturated"
OVERRIDE_EXCEPTIONAL = "quota_override_exceptional_discount"

EVENT_DEFERRED = "outbox_item_deferred"
EVENT_PLAN = "pool_deficit_plan"
EVENT_OVERRIDE = "quota_override_exceptional_discount"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class QuotaConfig:
    enabled: bool = True
    max_category_pct: float = 40.0
    max_brand_pct: float = 20.0
    max_marketplace_pct: float = 75.0
    min_target_categories: int = 4
    defer_saturated: bool = True
    defer_minutes: int = 60
    defer_max_minutes: int = 240
    exceptional_discount_threshold: float = 70.0
    # tamaño mínimo de pending bajo el cual NO se difiere nada (para no
    # quedarse sin material publicable). Si pending < este valor, todo entra.
    min_pending_before_quota: int = 8


@dataclass
class PendingSnapshot:
    total: int
    category_counts: dict
    brand_counts: dict
    marketplace_counts: dict

    def pct(self, counts: dict, key: Optional[str]) -> float:
        if not self.total or key is None:
            return 0.0
        return 100.0 * counts.get(key, 0) / self.total


@dataclass
class AdmissionDecision:
    state: str                       # "pending" | "deferred"
    scheduled_for: Optional[str]     # ISO o None
    defer_reason: Optional[str]
    extra_payload: dict = field(default_factory=dict)


def load_pending_snapshot(db: sqlite3.Connection) -> PendingSnapshot:
    """Cuenta el pending actual por categoría/marca/marketplace normalizados."""
    rows = db.execute(
        "SELECT message_payload_json FROM outbox WHERE state='pending'"
    ).fetchall()
    cat: dict = {}
    brand: dict = {}
    mkt: dict = {}
    for r in rows:
        raw = r["message_payload_json"] if hasattr(r, "keys") else r[0]
        try:
            p = json.loads(raw)
        except Exception:
            continue
        title = p.get("title") or ""
        cn = infer_offer_category(title, p, p.get("marketplace"), p.get("source")).normalized
        bn = normalize_brand(p.get("brand"), title) or "-"
        mn = p.get("marketplace") or "unknown"
        cat[cn] = cat.get(cn, 0) + 1
        brand[bn] = brand.get(bn, 0) + 1
        mkt[mn] = mkt.get(mn, 0) + 1
    return PendingSnapshot(total=len(rows), category_counts=cat,
                           brand_counts=brand, marketplace_counts=mkt)


def _category_of(payload: dict) -> str:
    return infer_offer_category(
        payload.get("title") or "", payload,
        payload.get("marketplace"), payload.get("source"),
    ).normalized


def decide_admission(
    db: sqlite3.Connection,
    payload: dict,
    config: QuotaConfig,
    *,
    snapshot: Optional[PendingSnapshot] = None,
    emit_event: bool = True,
) -> AdmissionDecision:
    """Decide si un item entra como pending o deferred según cuotas.

    `payload` es el message_payload del item (ya validado por gates+descuento).
    NUNCA descarta. Devuelve siempre pending o deferred.
    """
    now = _now()
    if not config.enabled:
        return AdmissionDecision(state="pending", scheduled_for=None, defer_reason=None)

    snap = snapshot if snapshot is not None else load_pending_snapshot(db)

    # Si el pending está casi vacío, no diferir (mantener material publicable).
    if snap.total < config.min_pending_before_quota:
        return AdmissionDecision(state="pending", scheduled_for=None, defer_reason=None)

    category = _category_of(payload)
    brand = normalize_brand(payload.get("brand"), payload.get("title") or "") or "-"
    marketplace = payload.get("marketplace") or "unknown"
    discount = payload.get("discount_percent")

    # Excepción: descuento excepcional entra aunque esté saturado.
    is_exceptional = isinstance(discount, (int, float)) and discount >= config.exceptional_discount_threshold

    # Proyectar el pending CON este item para evaluar el porcentaje resultante.
    projected_total = snap.total + 1
    cat_pct = 100.0 * (snap.category_counts.get(category, 0) + 1) / projected_total
    brand_pct = 100.0 * (snap.brand_counts.get(brand, 0) + 1) / projected_total
    mkt_pct = 100.0 * (snap.marketplace_counts.get(marketplace, 0) + 1) / projected_total

    defer_reason: Optional[str] = None
    if config.defer_saturated:
        if cat_pct > config.max_category_pct:
            defer_reason = DEFER_REASON_CATEGORY
        elif brand != "-" and brand_pct > config.max_brand_pct:
            defer_reason = DEFER_REASON_BRAND
        elif mkt_pct > config.max_marketplace_pct and _has_marketplace_alternative(snap, marketplace):
            defer_reason = DEFER_REASON_MARKETPLACE

    if defer_reason is None:
        return AdmissionDecision(state="pending", scheduled_for=None, defer_reason=None)

    if is_exceptional:
        if emit_event:
            _emit(db, EVENT_OVERRIDE, "info", {
                "category": category, "brand": brand, "marketplace": marketplace,
                "discount_percent": discount, "would_defer_reason": defer_reason,
                "threshold": config.exceptional_discount_threshold,
            })
        return AdmissionDecision(
            state="pending", scheduled_for=None, defer_reason=None,
            extra_payload={"quota_override": OVERRIDE_EXCEPTIONAL},
        )

    scheduled_for = _iso(now + timedelta(minutes=config.defer_minutes))
    extra = {
        "defer_reason": defer_reason,
        "deferred_at": _iso(now),
        "category_normalized": category,
        "brand_normalized": brand,
        "marketplace": marketplace,
        "promotion_attempts": 0,
        "quota_snapshot": {
            "pending_total": snap.total,
            "category_pct": round(snap.pct(snap.category_counts, category), 1),
            "brand_pct": round(snap.pct(snap.brand_counts, brand), 1),
            "marketplace_pct": round(snap.pct(snap.marketplace_counts, marketplace), 1),
        },
    }
    if emit_event:
        _emit(db, EVENT_DEFERRED, "info", {
            "category": category, "brand": brand, "marketplace": marketplace,
            "reason": defer_reason, "discount_percent": discount,
            "scheduled_for": scheduled_for, "counts": {
                "category": snap.category_counts.get(category, 0),
                "brand": snap.brand_counts.get(brand, 0),
                "marketplace": snap.marketplace_counts.get(marketplace, 0),
                "pending_total": snap.total,
            },
            "thresholds": {
                "category_pct": config.max_category_pct,
                "brand_pct": config.max_brand_pct,
                "marketplace_pct": config.max_marketplace_pct,
            },
        })
    return AdmissionDecision(
        state="deferred", scheduled_for=scheduled_for,
        defer_reason=defer_reason, extra_payload=extra,
    )


def _has_marketplace_alternative(snap: PendingSnapshot, marketplace: str) -> bool:
    """True si hay items de OTRO marketplace en pending (hay alternativa)."""
    return any(m != marketplace and c > 0 for m, c in snap.marketplace_counts.items())


def _emit(db: sqlite3.Connection, kind: str, severity: str, payload: dict) -> None:
    try:
        db.execute(
            "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (kind, severity, json.dumps(payload, ensure_ascii=False), _iso(_now())),
        )
        db.commit()
    except Exception:
        logger.exception("outbox_admission: emit event %s falló", kind)


__all__ = [
    "QuotaConfig", "PendingSnapshot", "AdmissionDecision",
    "load_pending_snapshot", "decide_admission",
    "DEFER_REASON_CATEGORY", "DEFER_REASON_BRAND", "DEFER_REASON_MARKETPLACE",
    "EVENT_DEFERRED", "EVENT_PLAN", "EVENT_OVERRIDE",
]


# ---------------------------------------------------------------------------
# Helper de inserción uniforme usado por TODOS los enqueue sites.
# ---------------------------------------------------------------------------

def quota_config_from_settings(settings) -> QuotaConfig:
    """Construye un QuotaConfig desde Settings (con defaults seguros)."""
    g = lambda name, default: getattr(settings, name, default)
    return QuotaConfig(
        enabled=bool(g("outbox_category_quota_enabled", True)),
        max_category_pct=float(g("outbox_max_pending_category_pct", 40.0)),
        max_brand_pct=float(g("outbox_max_pending_brand_pct", 20.0)),
        max_marketplace_pct=float(g("outbox_max_pending_marketplace_pct", 75.0)),
        min_target_categories=int(g("outbox_min_target_categories", 4)),
        defer_saturated=bool(g("outbox_defer_saturated_categories", True)),
        defer_minutes=int(g("outbox_defer_minutes", 60)),
        defer_max_minutes=int(g("outbox_defer_max_minutes", 240)),
        exceptional_discount_threshold=float(g("outbox_exceptional_discount_threshold", 70.0)),
        min_pending_before_quota=int(g("outbox_min_pending_before_quota", 8)),
    )


_CACHED_CFG: Optional[QuotaConfig] = None


def load_quota_config() -> QuotaConfig:
    """Carga QuotaConfig desde get_settings() (cacheado). Si falla, defaults."""
    global _CACHED_CFG
    if _CACHED_CFG is not None:
        return _CACHED_CFG
    try:
        from ..config import get_settings
        _CACHED_CFG = quota_config_from_settings(get_settings())
    except Exception:
        logger.exception("load_quota_config: usando defaults")
        _CACHED_CFG = QuotaConfig()
    return _CACHED_CFG


def enqueue_with_quota(
    db: sqlite3.Connection,
    *,
    offer_id: int,
    outbox_type: str,
    payload: dict,
    config: Optional[QuotaConfig] = None,
    now_iso_fn=None,
) -> Optional[int]:
    """Inserta un item al outbox decidiendo pending vs deferred por cuotas.

    Devuelve el outbox id. Si `config` es None o está deshabilitado, inserta
    como pending (comportamiento legacy). NO descarta nunca.
    """
    cfg = config or QuotaConfig(enabled=False)
    now_iso_fn = now_iso_fn or (lambda: _iso(_now()))

    # Enriquecer SIEMPRE el payload con los 6 campos normalizados de diversidad
    # ANTES de cualquier decisión/persistencia, para que el outbox almacene
    # category_normalized confiable (no el breadcrumb crudo contaminado de ML).
    payload = dict(payload)
    try:
        payload.update(compute_diversity_metadata(payload))
    except Exception:
        logger.exception("enqueue_with_quota: compute_diversity_metadata falló")

    try:
        decision = decide_admission(db, payload, cfg)
    except Exception:
        logger.exception("enqueue_with_quota: decide_admission falló; pending por defecto")
        decision = AdmissionDecision(state="pending", scheduled_for=None, defer_reason=None)

    final_payload = dict(payload)
    if decision.extra_payload:
        final_payload.update(decision.extra_payload)

    cur = db.execute(
        "INSERT INTO outbox (offer_id, type, enqueued_at, scheduled_for, attempts, "
        "last_attempt_at, state, message_payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            offer_id,
            outbox_type,
            now_iso_fn(),
            decision.scheduled_for,
            0,
            None,
            decision.state,
            json.dumps(final_payload, ensure_ascii=False),
        ),
    )
    return cur.lastrowid


__all__ += ["quota_config_from_settings", "load_quota_config", "enqueue_with_quota"]
