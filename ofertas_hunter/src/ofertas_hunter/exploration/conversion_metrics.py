"""Métricas de conversión discovery → PDP → pending por marketplace/categoría.

Emite el evento `discovery_conversion_summary` agregando, sobre una ventana de
tiempo reciente, el embudo:

  deal_pages_visited → product_urls_extracted → pdp_visited →
  discount_50_plus → verified_previous_price → pending_inserted →
  deferred → discarded

Es **read-mostly**: lee de runtime_events/products/offers/outbox/visited_urls/
discarded_candidates y escribe UN evento de resumen. NO toca gates, precios,
outbox ni published_messages. Pensado para llamarse cada N minutos desde el
orquestador (o on-demand desde un script de validación).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

EVENT_CONVERSION = "discovery_conversion_summary"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class ConversionConfig:
    enabled: bool = True
    every_minutes: int = 30
    window_minutes: int = 60


def _scalar(db: sqlite3.Connection, sql: str, params: tuple) -> int:
    try:
        row = db.execute(sql, params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        logger.exception("conversion_metrics: query falló: %s", sql[:60])
        return 0


def compute_conversion(
    db: sqlite3.Connection,
    *,
    marketplace: str,
    since_iso: str,
) -> dict:
    """Calcula el embudo de conversión para un marketplace desde `since_iso`."""
    # 1. deal pages visitadas (eventos deal_page_scroll_applied de este mkt).
    deal_pages = _scalar(
        db,
        "SELECT COUNT(*) FROM runtime_events WHERE kind='deal_page_scroll_applied' "
        "AND created_at>=? AND json_extract(payload_json,'$.marketplace')=?",
        (since_iso, marketplace),
    )

    # 2. product URLs extraídas (frontier kind=product agregadas) — aprox por
    #    productos vistos por primera vez en la ventana.
    product_urls = _scalar(
        db,
        "SELECT COUNT(*) FROM products WHERE marketplace=? AND first_seen_at>=?",
        (marketplace, since_iso),
    )

    # 3. PDPs visitados (visited_urls del marketplace en la ventana).
    pdp_visited = _scalar(
        db,
        "SELECT COUNT(*) FROM visited_urls WHERE marketplace=? AND visited_at>=?",
        (marketplace, since_iso),
    )

    # 4. offers creadas en la ventana para el marketplace.
    offers_created = _scalar(
        db,
        "SELECT COUNT(*) FROM offers o JOIN products p ON p.id=o.product_id "
        "WHERE p.marketplace=? AND o.created_at>=?",
        (marketplace, since_iso),
    )

    # 5. descuento >= 50 (sobre offers de la ventana).
    discount_50_plus = _scalar(
        db,
        "SELECT COUNT(*) FROM offers o JOIN products p ON p.id=o.product_id "
        "WHERE p.marketplace=? AND o.created_at>=? AND o.discount_percent>=50",
        (marketplace, since_iso),
    )

    # 6. pending insertados + deferred + discarded (outbox por estado, ventana
    #    por enqueued_at). Usamos el payload.marketplace para filtrar.
    def _outbox_state(state: str) -> int:
        return _scalar(
            db,
            "SELECT COUNT(*) FROM outbox WHERE state=? AND enqueued_at>=? "
            "AND json_extract(message_payload_json,'$.marketplace')=?",
            (state, since_iso, marketplace),
        )

    pending_inserted = _outbox_state("pending")
    deferred = _outbox_state("deferred")
    sent = _outbox_state("sent")

    # 7. verified_previous_price: en pending/deferred/sent del marketplace con
    #    previous_price verificado (ML usa ml_previous_price_verified; Amazon,
    #    previous_price no nulo). Aproximación segura sobre la ventana.
    if marketplace == "mercadolibre":
        verified_prev = _scalar(
            db,
            "SELECT COUNT(*) FROM outbox WHERE enqueued_at>=? "
            "AND json_extract(message_payload_json,'$.marketplace')='mercadolibre' "
            "AND json_extract(message_payload_json,'$.ml_previous_price_verified')=1",
            (since_iso,),
        )
    else:
        verified_prev = _scalar(
            db,
            "SELECT COUNT(*) FROM outbox WHERE enqueued_at>=? "
            "AND json_extract(message_payload_json,'$.marketplace')=? "
            "AND json_extract(message_payload_json,'$.previous_price') IS NOT NULL",
            (since_iso, marketplace),
        )

    # 8. discarded (candidatos descartados del marketplace en la ventana).
    discarded = _scalar(
        db,
        "SELECT COUNT(*) FROM discarded_candidates WHERE created_at>=? "
        "AND (source LIKE ? OR raw_payload_json LIKE ?)",
        (since_iso, f"%{marketplace}%", f'%"marketplace": "{marketplace}"%'),
    )

    return {
        "marketplace": marketplace,
        "deal_pages_visited": deal_pages,
        "product_urls_extracted": product_urls,
        "pdp_visited": pdp_visited,
        "offers_created": offers_created,
        "discount_50_plus": discount_50_plus,
        "verified_previous_price": verified_prev,
        "pending_inserted": pending_inserted,
        "deferred": deferred,
        "sent": sent,
        "discarded": discarded,
    }


def emit_conversion_summary(
    db: sqlite3.Connection,
    *,
    window_minutes: int = 60,
    marketplaces: tuple[str, ...] = ("amazon", "mercadolibre"),
) -> list[dict]:
    """Calcula y emite `discovery_conversion_summary` por marketplace.

    Devuelve la lista de summaries (también útil para el script de validación).
    """
    since = _iso(_now() - timedelta(minutes=window_minutes))
    summaries: list[dict] = []
    for mp in marketplaces:
        summary = compute_conversion(db, marketplace=mp, since_iso=since)
        summary["window_minutes"] = window_minutes
        summaries.append(summary)
        try:
            db.execute(
                "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (EVENT_CONVERSION, "info", json.dumps(summary, ensure_ascii=False), _iso(_now())),
            )
        except Exception:
            logger.exception("conversion_metrics: emit falló para %s", mp)
    try:
        db.commit()
    except Exception:
        pass
    return summaries


def maybe_emit_conversion(
    db: sqlite3.Connection,
    config: ConversionConfig,
) -> Optional[list[dict]]:
    """Emite el resumen si pasó `every_minutes` desde el último evento."""
    if not config.enabled:
        return None
    try:
        row = db.execute(
            "SELECT created_at FROM runtime_events WHERE kind=? ORDER BY id DESC LIMIT 1",
            (EVENT_CONVERSION,),
        ).fetchone()
        if row is not None:
            last_str = row[0] if not hasattr(row, "keys") else row["created_at"]
            try:
                last = datetime.fromisoformat(str(last_str).replace("Z", "+00:00"))
                if (_now() - last).total_seconds() < config.every_minutes * 60:
                    return None
            except Exception:
                pass
        return emit_conversion_summary(db, window_minutes=config.window_minutes)
    except Exception:
        logger.exception("conversion_metrics: maybe_emit falló")
        return None


__all__ = [
    "ConversionConfig",
    "EVENT_CONVERSION",
    "compute_conversion",
    "emit_conversion_summary",
    "maybe_emit_conversion",
]
