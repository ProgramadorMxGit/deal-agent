"""Auditoría retroactiva de falsos `price_error` por accesorios genéricos.

Implementa el comando:

    python -m ofertas_hunter audit-false-price-errors --fix

Pasos:

1. Lee `outbox` y (opcionalmente) `published_messages` recientes con
   `type='price_error'` o clasificación equivalente en su payload.
2. Aplica `assess_title()` a cada título para detectar accesorios genéricos.
3. Para cada coincidencia:
   - Emite un `runtime_event(kind='false_price_error_detected')` con
     contexto (outbox_id, title, price, marketplace, reason).
   - Si `--fix` y el item está en estado `pending` o `in_flight`, lo
     degrada o descarta según REGLA 6 (lo mismo que hace el dispatcher).
   - Si fue ya `sent` (publicado), sólo registra el aprendizaje (memoria
     negativa) — no se puede deshacer.

El módulo es puro (recibe la conexión SQLite); el CLI se ocupa de
construirla y cerrarla.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..models import OutboxState, OutboxType
from ..runtime.events import emit_runtime_event
from .accessory_detector import AccessoryAssessment, assess_title


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------


@dataclass
class AuditFinding:
    outbox_id: int
    state: str
    type: str
    title: str
    current_price: Optional[float]
    marketplace: Optional[str]
    confidence_label: Optional[str]
    discount_percent: Optional[float]
    is_generic_accessory: bool
    mentions_compatible_with_premium: bool
    matched_tokens: tuple[str, ...]
    action: str  # "logged_only" | "discarded" | "degraded_to_normal"
    reason: str


@dataclass
class AuditReport:
    scanned: int = 0
    findings: list[AuditFinding] = field(default_factory=list)
    discarded: int = 0
    degraded: int = 0
    logged_only: int = 0


# ---------------------------------------------------------------------------
# API principal
# ---------------------------------------------------------------------------


def run_audit(
    conn: sqlite3.Connection,
    *,
    fix: bool = False,
    days: int = 7,
    limit: Optional[int] = None,
) -> AuditReport:
    """Recorre outbox + published_messages recientes y detecta falsos PE.

    Args:
        conn: conexión SQLite ya inicializada.
        fix: si True, aplica correcciones (degrade / discard) sobre items
            todavía no publicados. Si False, sólo audita.
        days: ventana de tiempo (en días) para mirar atrás.
        limit: tope opcional de items a revisar.
    """
    report = AuditReport()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max(1, days))
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    rows = _select_pe_outbox(conn, cutoff=cutoff, limit=limit)
    report.scanned = len(rows)

    for row in rows:
        finding = _audit_row(conn, row=row, fix=fix)
        if finding is None:
            continue
        report.findings.append(finding)
        if finding.action == "discarded":
            report.discarded += 1
        elif finding.action == "degraded_to_normal":
            report.degraded += 1
        else:
            report.logged_only += 1

    return report


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _select_pe_outbox(
    conn: sqlite3.Connection, *, cutoff: str, limit: Optional[int]
) -> list[dict]:
    sql = (
        "SELECT id, offer_id, type, state, message_payload_json, enqueued_at "
        "FROM outbox "
        "WHERE type IN (?, ?) AND enqueued_at >= ? "
        "ORDER BY id DESC"
    )
    params: list = [
        OutboxType.PRICE_ERROR.value,
        OutboxType.POSSIBLE_PE.value,
        cutoff,
    ]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    cursor = conn.execute(sql, params)
    rows: list[dict] = []
    for r in cursor.fetchall():
        try:
            payload = json.loads(r["message_payload_json"])
        except Exception:
            payload = {}
        rows.append(
            {
                "id": r["id"],
                "offer_id": r["offer_id"],
                "type": r["type"],
                "state": r["state"],
                "payload": payload,
            }
        )
    return rows


def _audit_row(
    conn: sqlite3.Connection, *, row: dict, fix: bool
) -> Optional[AuditFinding]:
    payload = row["payload"] or {}
    title: str = payload.get("title") or ""
    marketplace: Optional[str] = payload.get("marketplace")
    confidence_label: Optional[str] = payload.get("confidence_label")
    current_price = payload.get("current_price")
    discount_percent = payload.get("discount_percent")

    if not title:
        return None

    assessment: AccessoryAssessment = assess_title(
        title,
        category=payload.get("category"),
        brand=payload.get("brand"),
    )
    if not (
        assessment.is_generic_accessory
        or assessment.mentions_compatible_with_premium
    ):
        return None

    # Es un falso PE candidato. Decidir acción según estado y discount.
    state = row["state"]
    action = "logged_only"
    reason = "false_price_error_generic_accessory"

    # Sólo modificamos items que aún no se enviaron.
    if fix and state in (OutboxState.PENDING.value, OutboxState.IN_FLIGHT.value):
        try:
            disc = float(discount_percent) if discount_percent is not None else None
        except (TypeError, ValueError):
            disc = None
        if disc is not None and disc >= 50.0:
            _degrade_to_normal(conn, row=row, discount=disc)
            action = "degraded_to_normal"
            reason = "false_pe_with_real_discount_>=50_degraded_to_normal"
        else:
            _mark_discarded(conn, row=row, reason="false_price_error_generic_accessory")
            action = "discarded"
            reason = "false_price_error_generic_accessory_no_discount"

    # Aprendizaje: emitimos runtime_event siempre (memoria negativa).
    try:
        emit_runtime_event(
            conn,
            kind="false_price_error_detected",
            severity="warning",
            payload={
                "outbox_id": row["id"],
                "offer_id": row["offer_id"],
                "title": title,
                "current_price": current_price,
                "marketplace": marketplace,
                "confidence_label": confidence_label,
                "discount_percent": discount_percent,
                "is_generic_accessory": assessment.is_generic_accessory,
                "mentions_compatible_with_premium": assessment.mentions_compatible_with_premium,
                "matched_tokens": list(assessment.matched_tokens),
                "audit_action": action,
            },
        )
        if fix:
            conn.commit()
    except Exception as exc:  # nunca dejar caer la auditoría
        logger.warning("audit emit_runtime_event failed: %s", exc)

    return AuditFinding(
        outbox_id=row["id"],
        state=state,
        type=row["type"],
        title=title,
        current_price=current_price if isinstance(current_price, (int, float)) else None,
        marketplace=marketplace,
        confidence_label=confidence_label,
        discount_percent=(
            float(discount_percent)
            if isinstance(discount_percent, (int, float))
            else None
        ),
        is_generic_accessory=assessment.is_generic_accessory,
        mentions_compatible_with_premium=assessment.mentions_compatible_with_premium,
        matched_tokens=assessment.matched_tokens,
        action=action,
        reason=reason,
    )


def _mark_discarded(
    conn: sqlite3.Connection, *, row: dict, reason: str
) -> None:
    """Marca un outbox item como `discarded` registrando la razón en payload."""
    payload = dict(row["payload"] or {})
    payload["discarded_reason"] = reason
    payload.setdefault("audit_history", []).append(
        {
            "kind": "audit_false_price_error",
            "action": "discarded",
            "reason": reason,
            "at": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        }
    )
    conn.execute(
        "UPDATE outbox SET state=?, message_payload_json=? WHERE id=?",
        (OutboxState.DISCARDED.value, json.dumps(payload, ensure_ascii=False), row["id"]),
    )


def _degrade_to_normal(
    conn: sqlite3.Connection, *, row: dict, discount: float
) -> None:
    """Re-tipifica el outbox como `normal`, completando previous_price si falta."""
    payload = dict(row["payload"] or {})
    current = payload.get("current_price")
    if payload.get("previous_price") is None:
        try:
            current_f = float(current) if current is not None else None
            if current_f and current_f > 0:
                payload["previous_price"] = round(
                    current_f / (1 - discount / 100.0), 2
                )
        except (TypeError, ValueError):
            pass
    payload["discount_percent"] = float(discount)
    payload["degraded_from"] = row["type"]
    payload["degraded_reason"] = "audit_false_pe_with_real_discount"
    payload.setdefault("audit_history", []).append(
        {
            "kind": "audit_false_price_error",
            "action": "degraded_to_normal",
            "discount_percent": float(discount),
            "at": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        }
    )
    conn.execute(
        "UPDATE outbox SET type=?, message_payload_json=? WHERE id=?",
        (
            OutboxType.NORMAL.value,
            json.dumps(payload, ensure_ascii=False),
            row["id"],
        ),
    )


__all__ = ["AuditFinding", "AuditReport", "run_audit"]
