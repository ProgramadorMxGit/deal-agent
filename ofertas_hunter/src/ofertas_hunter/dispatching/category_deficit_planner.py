"""Planner de déficit de categorías + promoción de items deferred.

Fase 1: `compute_plan` analiza pending/deferred/sent y produce un
`pool_deficit_plan` (categorías saturadas vs deficitarias, marcas/marketplaces
saturados, categorías recomendadas para el frontier).

Fase 4: `promote_deferred` mueve items de `deferred` a `pending` cuando la
categoría/marca/marketplace ya no está saturada, o degradado tras max minutos
para no quedarse sin material publicable.

Todo es read-mostly; las escrituras son solo en outbox.state (deferred->pending)
y runtime_events. NO toca gates, sent, discarded ni published.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .outbox_admission import (
    PendingSnapshot,
    QuotaConfig,
    load_pending_snapshot,
)
from ..dispatching.diversity_metadata import infer_offer_category, normalize_brand

logger = logging.getLogger(__name__)

EVENT_PLAN = "pool_deficit_plan"
EVENT_PROMOTED = "outbox_deferred_promoted"
EVENT_PROMOTED_DEGRADED = "outbox_deferred_promoted_degraded"
EVENT_BALANCE = "pending_pool_balance_summary"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class PlannerConfig:
    enabled: bool = True
    target_categories: tuple = (
        "tecnologia", "hogar", "bebe", "herramientas", "ropa",
        "despensa", "juguetes", "mascotas", "belleza", "proteina/suplementos",
    )
    plan_every_seconds: int = 300


def compute_plan(
    db: sqlite3.Connection,
    planner_cfg: PlannerConfig,
    quota_cfg: QuotaConfig,
    *,
    snapshot: Optional[PendingSnapshot] = None,
) -> dict:
    """Calcula el plan de déficit a partir del pending actual."""
    snap = snapshot if snapshot is not None else load_pending_snapshot(db)
    total = snap.total or 1

    saturated_categories = [
        c for c, n in snap.category_counts.items()
        if 100.0 * n / total > quota_cfg.max_category_pct
    ]
    saturated_brands = [
        b for b, n in snap.brand_counts.items()
        if b != "-" and 100.0 * n / total > quota_cfg.max_brand_pct
    ]
    saturated_marketplaces = [
        m for m, n in snap.marketplace_counts.items()
        if 100.0 * n / total > quota_cfg.max_marketplace_pct
    ]

    present = set(snap.category_counts.keys())
    deficit_categories = [
        c for c in planner_cfg.target_categories
        if snap.category_counts.get(c, 0) == 0 or c not in saturated_categories
    ]
    # Recomendadas para frontier: las del target que NO están saturadas,
    # ordenadas por menor presencia primero.
    recommended = sorted(
        [c for c in planner_cfg.target_categories if c not in saturated_categories],
        key=lambda c: snap.category_counts.get(c, 0),
    )

    plan = {
        "pending_total": snap.total,
        "category_counts": dict(sorted(snap.category_counts.items(), key=lambda x: -x[1])),
        "brand_counts": dict(sorted(snap.brand_counts.items(), key=lambda x: -x[1])),
        "marketplace_counts": dict(snap.marketplace_counts),
        "saturated_categories": saturated_categories,
        "deficit_categories": deficit_categories,
        "saturated_brands": saturated_brands,
        "saturated_marketplaces": saturated_marketplaces,
        "recommended_frontier_categories": recommended[:6],
        "mode": "strict_50_discount",
    }
    return plan


def maybe_emit_plan(
    db: sqlite3.Connection,
    planner_cfg: PlannerConfig,
    quota_cfg: QuotaConfig,
) -> Optional[dict]:
    """Emite pool_deficit_plan si pasó plan_every_seconds desde el último."""
    if not planner_cfg.enabled:
        return None
    try:
        row = db.execute(
            "SELECT created_at FROM runtime_events WHERE kind=? ORDER BY id DESC LIMIT 1",
            (EVENT_PLAN,),
        ).fetchone()
        if row is not None:
            last_str = row["created_at"] if hasattr(row, "keys") else row[0]
            try:
                last = datetime.fromisoformat(str(last_str).replace("Z", "+00:00"))
                if (_now() - last).total_seconds() < planner_cfg.plan_every_seconds:
                    return None
            except Exception:
                pass
        plan = compute_plan(db, planner_cfg, quota_cfg)
        db.execute(
            "INSERT INTO runtime_events (kind, severity, payload_json, created_at) VALUES (?,?,?,?)",
            (EVENT_PLAN, "info", json.dumps(plan, ensure_ascii=False), _iso(_now())),
        )
        db.commit()
        return plan
    except Exception:
        logger.exception("category_deficit_planner: maybe_emit_plan falló")
        return None


def get_latest_plan(db: sqlite3.Connection, max_age_seconds: int = 600) -> Optional[dict]:
    """Devuelve el último pool_deficit_plan si es reciente, si no None."""
    try:
        row = db.execute(
            "SELECT payload_json, created_at FROM runtime_events WHERE kind=? ORDER BY id DESC LIMIT 1",
            (EVENT_PLAN,),
        ).fetchone()
        if not row:
            return None
        created = row["created_at"] if hasattr(row, "keys") else row[1]
        try:
            age = (_now() - datetime.fromisoformat(str(created).replace("Z", "+00:00"))).total_seconds()
            if age > max_age_seconds:
                return None
        except Exception:
            return None
        raw = row["payload_json"] if hasattr(row, "keys") else row[0]
        return json.loads(raw)
    except Exception:
        return None


def promote_deferred(
    db: sqlite3.Connection,
    quota_cfg: QuotaConfig,
    *,
    limit: int = 20,
) -> dict:
    """Promueve items deferred -> pending. Devuelve resumen.

    Reglas:
    - Promueve si la categoría/marca/marketplace ya NO supera su cuota.
    - Promueve degradado si pasaron defer_max_minutes (no perder ofertas) o si
      el pending está por debajo del mínimo (mantener despacho).
    """
    now = _now()
    promoted = 0
    promoted_degraded = 0
    snap = load_pending_snapshot(db)

    rows = db.execute(
        "SELECT id, message_payload_json, enqueued_at, scheduled_for "
        "FROM outbox WHERE state='deferred' ORDER BY id ASC LIMIT ?",
        (limit,),
    ).fetchall()

    for r in rows:
        rid = r["id"] if hasattr(r, "keys") else r[0]
        raw = r["message_payload_json"] if hasattr(r, "keys") else r[1]
        try:
            p = json.loads(raw)
        except Exception:
            continue
        title = p.get("title") or ""
        category = infer_offer_category(title, p, p.get("marketplace"), p.get("source")).normalized
        brand = normalize_brand(p.get("brand"), title) or "-"
        marketplace = p.get("marketplace") or "unknown"
        deferred_at = p.get("deferred_at")

        # ¿sigue saturado?
        projected = snap.total + 1
        cat_pct = 100.0 * (snap.category_counts.get(category, 0) + 1) / projected
        brand_pct = 100.0 * (snap.brand_counts.get(brand, 0) + 1) / projected
        mkt_pct = 100.0 * (snap.marketplace_counts.get(marketplace, 0) + 1) / projected
        still_saturated = (
            cat_pct > quota_cfg.max_category_pct
            or (brand != "-" and brand_pct > quota_cfg.max_brand_pct)
            or mkt_pct > quota_cfg.max_marketplace_pct
        )

        # ¿degradado por tiempo o por pending bajo?
        aged_out = False
        if deferred_at:
            try:
                da = datetime.fromisoformat(str(deferred_at).replace("Z", "+00:00"))
                aged_out = (now - da).total_seconds() >= quota_cfg.defer_max_minutes * 60
            except Exception:
                aged_out = False
        pending_low = snap.total < quota_cfg.min_pending_before_quota

        degraded = False
        if not still_saturated:
            do_promote = True
        elif aged_out or pending_low:
            do_promote = True
            degraded = True
        else:
            do_promote = False

        if not do_promote:
            continue

        # actualizar payload (promotion_attempts, promoted flags)
        p["promotion_attempts"] = int(p.get("promotion_attempts", 0)) + 1
        p["promoted_at"] = _iso(now)
        p["promoted_degraded"] = degraded
        try:
            db.execute(
                "UPDATE outbox SET state='pending', scheduled_for=NULL, "
                "enqueued_at=?, message_payload_json=? WHERE id=? AND state='deferred'",
                (_iso(now), json.dumps(p, ensure_ascii=False), rid),
            )
        except Exception:
            logger.exception("promote_deferred: update falló id=%s", rid)
            continue

        # actualizar snapshot local para que las cuotas se respeten en el bucle
        snap.category_counts[category] = snap.category_counts.get(category, 0) + 1
        snap.brand_counts[brand] = snap.brand_counts.get(brand, 0) + 1
        snap.marketplace_counts[marketplace] = snap.marketplace_counts.get(marketplace, 0) + 1
        snap.total += 1

        if degraded:
            promoted_degraded += 1
            _emit(db, EVENT_PROMOTED_DEGRADED, {
                "outbox_id": rid, "category": category, "brand": brand,
                "marketplace": marketplace, "reason": "aged_out" if aged_out else "pending_low",
            })
        else:
            promoted += 1
            _emit(db, EVENT_PROMOTED, {
                "outbox_id": rid, "category": category, "brand": brand,
                "marketplace": marketplace,
            })

    if promoted or promoted_degraded:
        try:
            db.commit()
        except Exception:
            logger.exception("promote_deferred: commit falló")
    return {"promoted": promoted, "promoted_degraded": promoted_degraded,
            "deferred_seen": len(rows)}


def _emit(db: sqlite3.Connection, kind: str, payload: dict) -> None:
    try:
        db.execute(
            "INSERT INTO runtime_events (kind, severity, payload_json, created_at) VALUES (?,?,?,?)",
            (kind, "info", json.dumps(payload, ensure_ascii=False), _iso(_now())),
        )
    except Exception:
        logger.exception("planner: emit %s falló", kind)


__all__ = [
    "PlannerConfig", "compute_plan", "maybe_emit_plan", "get_latest_plan",
    "promote_deferred", "EVENT_PLAN", "EVENT_PROMOTED", "EVENT_PROMOTED_DEGRADED",
]
