"""Serializers JSON-safe para los modelos del bot.

Transforman dataclasses y filas SQLite en `dict` planos listos para devolver
por MCP. Reglas:

- `datetime` → ISO 8601 UTC con sufijo "Z".
- `Decimal` / floats → `float` plano.
- `Enum` → su `.value`.
- `None` se preserva.
- Las dataclasses se convierten campo a campo (no usamos `asdict` para
  controlar qué exponemos al cliente).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from ..models import OutboxItem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _safe_decimal(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    return value


def _maybe_loads(value: Any) -> Any:
    """Si el valor es un string JSON, parsea; si no, devuelve tal cual."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


# ---------------------------------------------------------------------------
# OutboxItem
# ---------------------------------------------------------------------------


def serialize_outbox_item(item: OutboxItem) -> dict:
    """Convierte un `OutboxItem` (dataclass) a dict JSON-safe."""
    return {
        "id": item.id,
        "offer_id": item.offer_id,
        "type": item.type,
        "state": item.state,
        "enqueued_at": _iso(item.enqueued_at),
        "scheduled_for": _iso(item.scheduled_for),
        "attempts": item.attempts,
        "last_attempt_at": _iso(item.last_attempt_at),
        "payload": item.message_payload or {},
    }


def serialize_outbox_row(row: sqlite3.Row | dict) -> dict:
    """Idéntico al anterior pero para filas crudas de SQLite."""
    d = dict(row)
    return {
        "id": d.get("id"),
        "offer_id": d.get("offer_id"),
        "type": d.get("type"),
        "state": d.get("state"),
        "enqueued_at": d.get("enqueued_at"),
        "scheduled_for": d.get("scheduled_for"),
        "attempts": d.get("attempts", 0),
        "last_attempt_at": d.get("last_attempt_at"),
        "payload": _maybe_loads(d.get("message_payload_json")) or {},
    }


# ---------------------------------------------------------------------------
# Offer
# ---------------------------------------------------------------------------


def serialize_offer_row(row: sqlite3.Row | dict) -> dict:
    d = dict(row)
    return {
        "id": d.get("id"),
        "product_id": d.get("product_id"),
        "classification": d.get("classification"),
        "score": d.get("score"),
        "discount_percent": _safe_decimal(d.get("discount_percent")),
        "state": d.get("state"),
        "reasons": _maybe_loads(d.get("reasons_json")) or [],
        "current_price_observation_id": d.get("current_price_observation_id"),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
    }


# ---------------------------------------------------------------------------
# RuntimeEvent
# ---------------------------------------------------------------------------


def serialize_runtime_event_row(row: sqlite3.Row | dict) -> dict:
    d = dict(row)
    return {
        "id": d.get("id"),
        "kind": d.get("kind"),
        "severity": d.get("severity"),
        "payload": _maybe_loads(d.get("payload_json")) or {},
        "created_at": d.get("created_at"),
        "acknowledged_at": d.get("acknowledged_at"),
    }


# ---------------------------------------------------------------------------
# ScheduleDecision
# ---------------------------------------------------------------------------


def serialize_schedule_decision(decision: Any) -> dict:
    """Acepta `ModeDecision` del scheduler."""
    mode = decision.mode
    next_mode = decision.next_mode
    return {
        "mode": mode.value if isinstance(mode, Enum) else str(mode),
        "next_mode": next_mode.value if isinstance(next_mode, Enum) else str(next_mode),
        "next_change_in_seconds": int(decision.next_change_in.total_seconds()),
        "local_time": decision.local_time.isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# PublishOutcome
# ---------------------------------------------------------------------------


def serialize_publish_outcome(outcome: Any) -> dict:
    """Acepta `PublishOutcome` del WhatsAppPublisher."""
    formatted = outcome.formatted
    formatted_summary = None
    if formatted is not None:
        formatted_summary = {
            "type": formatted.type,
            "image_url": formatted.image_url,
            "text_length": len(formatted.text or ""),
        }

    evolution = outcome.evolution_response
    evolution_summary = None
    if evolution is not None:
        evolution_summary = {
            "success": evolution.success,
            "dry_run": evolution.dry_run,
            "status_code": getattr(evolution, "status_code", None),
            "error": getattr(evolution, "error", None),
        }

    return {
        "success": outcome.success,
        "dry_run": outcome.dry_run,
        "skipped": outcome.skipped,
        "skip_reason": outcome.skip_reason,
        "error": outcome.error,
        "formatted": formatted_summary,
        "evolution": evolution_summary,
    }


# ---------------------------------------------------------------------------
# RevalidationDetail
# ---------------------------------------------------------------------------


def serialize_revalidation(detail: Any) -> dict:
    extracted = getattr(detail, "extracted", None)
    extracted_summary = None
    if extracted is not None:
        extracted_summary = {
            "title": getattr(extracted, "title", None),
            "current_price": _safe_decimal(getattr(extracted, "current_price", None)),
            "previous_price": _safe_decimal(getattr(extracted, "previous_price", None)),
            "image_url": getattr(extracted, "image_url", None),
            "in_stock": getattr(extracted, "in_stock", None),
            "monthly_payment_suspected": getattr(
                extracted, "monthly_payment_suspected", False
            ),
            "variant_mismatch": getattr(extracted, "variant_mismatch", False),
        }

    return {
        "ok": detail.ok,
        "classification": detail.classification,
        "fatal_reason": detail.fatal_reason,
        "reasons": list(detail.reasons or []),
        "snapshot_saved": detail.snapshot_saved,
        "suggested_outbox_type": detail.suggested_outbox_type,
        "confidence_label": detail.confidence_label,
        "extracted": extracted_summary,
    }


__all__ = [
    "serialize_outbox_item",
    "serialize_outbox_row",
    "serialize_offer_row",
    "serialize_runtime_event_row",
    "serialize_schedule_decision",
    "serialize_publish_outcome",
    "serialize_revalidation",
]
