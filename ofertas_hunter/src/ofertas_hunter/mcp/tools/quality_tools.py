"""Quality gate tools del MCP server.

Cuatro tools en patrón request → submit:

- `request_offer_review(outbox_id)` → {review_token, outbox, formatted_preview}
- `submit_offer_review(outbox_id, review_token, decision, reason?, new_text?)`
- `improve_message_copy(outbox_id, current_text)` → {review_token, outbox, current_text}
- `submit_message_copy(outbox_id, review_token, new_text)`

`request_*` aplica los gates duros (image_price_url, ml_affiliate, telegram_to_ml)
ANTES de crear la sesión. `submit_*` valida el token y aplica la decisión:

- approve: deja `state=pending` y NO modifica payload.
- reject: marca `state=discarded` y persiste razón.
- rewrite_message / submit_message_copy: SOLO modifica `payload["caption_override"]`.
  El formatter respeta `caption_override` cuando está presente, así preservamos
  todos los campos materiales (image, url, prices, marketplace, score).
"""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ...models import OutboxItem, OutboxState
from ...publishing.formatter import (
    FormatterError,
    format_normal_offer,
    format_price_error,
)
from ...runtime.events import emit_runtime_event
from ..context import ReviewSession
from . import ToolSpec


logger = logging.getLogger(__name__)


_DECISIONS = ("approve", "reject", "rewrite_message")
REVIEW_TTL_MINUTES = 10


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------


def _load_outbox_item(db: sqlite3.Connection, outbox_id: int) -> Optional[OutboxItem]:
    row = db.execute(
        "SELECT id, offer_id, type, enqueued_at, scheduled_for, attempts, "
        "last_attempt_at, state, message_payload_json FROM outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()
    if row is None:
        return None
    payload = row["message_payload_json"]
    try:
        payload_dict = json.loads(payload) if payload else {}
    except (TypeError, ValueError):
        payload_dict = {}
    return OutboxItem(
        offer_id=row["offer_id"],
        type=row["type"],
        message_payload=payload_dict,
        attempts=row["attempts"] or 0,
        state=row["state"],
        id=row["id"],
    )


def _preview_message(item: OutboxItem) -> dict:
    """Genera un preview del mensaje formateado o uno textual si hay caption_override."""
    payload = item.message_payload or {}
    caption_override = payload.get("caption_override")
    if caption_override:
        return {
            "type": item.type,
            "image_url": payload.get("image_url"),
            "text": caption_override,
            "uses_override": True,
        }
    try:
        if item.type == "normal":
            msg = format_normal_offer(
                title=payload.get("title", ""),
                current_price=float(payload.get("current_price", 0)),
                previous_price=float(payload.get("previous_price", 0)),
                discount_percent=float(payload.get("discount_percent", 0)),
                url=payload.get("affiliate_url") or payload.get("url") or "",
                image_url=payload.get("image_url") or "",
            )
        else:
            msg = format_price_error(
                title=payload.get("title", ""),
                current_price=float(payload.get("current_price", 0)),
                confidence_label=payload.get("confidence_label", "high"),
                marketplace=payload.get("marketplace", "other"),
                url=payload.get("affiliate_url") or payload.get("url") or "",
                image_url=payload.get("image_url") or "",
            )
        return {
            "type": msg.type,
            "image_url": msg.image_url,
            "text": msg.text,
            "uses_override": False,
        }
    except FormatterError as exc:
        return {
            "type": item.type,
            "image_url": payload.get("image_url"),
            "text": None,
            "uses_override": False,
            "formatter_error": str(exc),
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_review_session(item: OutboxItem, kind: str) -> ReviewSession:
    return ReviewSession(
        token=uuid.uuid4().hex,
        outbox_id=item.id or 0,
        kind=kind,
        snapshot_payload=copy.deepcopy(item.message_payload or {}),
        created_at=_now(),
        expires_at=_now() + timedelta(minutes=REVIEW_TTL_MINUTES),
    )


def _validate_token(
    ctx: Any, *, token: str, outbox_id: int, expected_kind: str
) -> Optional[ReviewSession]:
    session = ctx.review_tokens.get(token)
    if session is None:
        return None
    if session.expires_at < _now():
        ctx.review_tokens.pop(token, None)
        return None
    if session.outbox_id != outbox_id:
        return None
    if session.kind != expected_kind:
        return None
    return session


def _mark_eligible(db: sqlite3.Connection, outbox_id: int) -> None:
    db.execute(
        "UPDATE outbox SET state = ? WHERE id = ?",
        (OutboxState.PENDING.value, outbox_id),
    )
    db.commit()


def _mark_discarded(db: sqlite3.Connection, outbox_id: int, reason: str) -> None:
    db.execute(
        "UPDATE outbox SET state = ? WHERE id = ?",
        (OutboxState.DISCARDED.value, outbox_id),
    )
    db.execute(
        "INSERT INTO discarded_candidates (source, raw_payload_json, reason, created_at) "
        "VALUES (?, ?, ?, ?)",
        (
            "mcp_review",
            json.dumps({"outbox_id": outbox_id, "reason": reason}),
            f"mcp_review:{reason}",
            _now().isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        ),
    )
    db.commit()


def _apply_rewrite(
    db: sqlite3.Connection,
    outbox_id: int,
    snapshot_payload: dict,
    new_text: str,
) -> dict:
    """Modifica SOLO `caption_override` en el payload. Preserva todo lo demás."""
    new_payload = copy.deepcopy(snapshot_payload)
    new_payload["caption_override"] = new_text
    db.execute(
        "UPDATE outbox SET message_payload_json = ? WHERE id = ?",
        (json.dumps(new_payload, ensure_ascii=False), outbox_id),
    )
    db.commit()
    return new_payload


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _h_request_offer_review(ctx: Any, args: dict) -> dict:
    outbox_id = int(args["outbox_id"])
    item = _load_outbox_item(ctx.db, outbox_id)
    if item is None:
        return {"error": "outbox_not_found", "outbox_id": outbox_id}
    session = _new_review_session(item, kind="offer_review")
    ctx.review_tokens[session.token] = session
    return {
        "review_token": session.token,
        "expires_at": session.expires_at.isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "outbox": {
            "id": item.id,
            "type": item.type,
            "state": item.state,
            "payload": item.message_payload,
        },
        "formatted_preview": _preview_message(item),
    }


async def _h_submit_offer_review(ctx: Any, args: dict) -> dict:
    outbox_id = int(args["outbox_id"])
    token = args["review_token"]
    decision = args["decision"]

    session = _validate_token(
        ctx, token=token, outbox_id=outbox_id, expected_kind="offer_review"
    )
    if session is None:
        return {"error": "invalid_or_expired_token"}

    item = _load_outbox_item(ctx.db, outbox_id)
    if item is None:
        return {"error": "outbox_not_found", "outbox_id": outbox_id}

    if decision == "approve":
        _mark_eligible(ctx.db, outbox_id)
        ctx.review_tokens.pop(token, None)
        emit_runtime_event(
            ctx.db,
            kind="mcp_offer_reviewed",
            severity="info",
            payload={"outbox_id": outbox_id, "decision": "approve"},
        )
        return {"success": True, "decision": "approve", "outbox_id": outbox_id}

    if decision == "reject":
        reason = args.get("reason") or "rejected_by_review"
        _mark_discarded(ctx.db, outbox_id, reason)
        ctx.review_tokens.pop(token, None)
        emit_runtime_event(
            ctx.db,
            kind="mcp_offer_reviewed",
            severity="info",
            payload={"outbox_id": outbox_id, "decision": "reject", "reason": reason},
        )
        return {
            "success": True,
            "decision": "reject",
            "outbox_id": outbox_id,
            "reason": reason,
        }

    if decision == "rewrite_message":
        new_text = args.get("new_text")
        if not new_text:
            return {"error": "missing_new_text"}
        _apply_rewrite(ctx.db, outbox_id, session.snapshot_payload, new_text)
        ctx.review_tokens.pop(token, None)
        emit_runtime_event(
            ctx.db,
            kind="mcp_offer_reviewed",
            severity="info",
            payload={"outbox_id": outbox_id, "decision": "rewrite_message"},
        )
        return {"success": True, "decision": "rewrite_message", "outbox_id": outbox_id}

    return {"error": "unknown_decision"}


async def _h_improve_message_copy(ctx: Any, args: dict) -> dict:
    outbox_id = int(args["outbox_id"])
    current_text = args.get("current_text") or ""
    item = _load_outbox_item(ctx.db, outbox_id)
    if item is None:
        return {"error": "outbox_not_found", "outbox_id": outbox_id}
    session = _new_review_session(item, kind="message_copy")
    ctx.review_tokens[session.token] = session
    return {
        "review_token": session.token,
        "expires_at": session.expires_at.isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "outbox": {
            "id": item.id,
            "type": item.type,
            "payload": item.message_payload,
        },
        "current_text": current_text,
        "formatted_preview": _preview_message(item),
    }


async def _h_submit_message_copy(ctx: Any, args: dict) -> dict:
    outbox_id = int(args["outbox_id"])
    token = args["review_token"]
    new_text = args["new_text"]

    session = _validate_token(
        ctx, token=token, outbox_id=outbox_id, expected_kind="message_copy"
    )
    if session is None:
        return {"error": "invalid_or_expired_token"}

    item = _load_outbox_item(ctx.db, outbox_id)
    if item is None:
        return {"error": "outbox_not_found", "outbox_id": outbox_id}

    _apply_rewrite(ctx.db, outbox_id, session.snapshot_payload, new_text)
    ctx.review_tokens.pop(token, None)
    emit_runtime_event(
        ctx.db,
        kind="mcp_offer_reviewed",
        severity="info",
        payload={"outbox_id": outbox_id, "decision": "submit_message_copy"},
    )
    return {"success": True, "outbox_id": outbox_id}


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_quality_tools(ctx: Any) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="request_offer_review",
            description=(
                "Devuelve el OutboxItem completo y un preview del mensaje "
                "formateado, junto con un review_token. Aplica los gates duros "
                "(image_price_url, ml_affiliate, telegram_to_ml) ANTES de crear "
                "la sesión."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "outbox_id": {"type": "integer", "minimum": 1},
                },
                "required": ["outbox_id"],
                "additionalProperties": False,
            },
            handler=_h_request_offer_review,
            is_read_only=False,
            safety_rules=("image_price_url", "ml_affiliate", "telegram_to_ml"),
        ),
        ToolSpec(
            name="submit_offer_review",
            description=(
                "Aplica la decisión de revisión. approve = state pending sin "
                "modificar payload. reject = state discarded con razón. "
                "rewrite_message = actualiza SOLO caption_override (preserva "
                "image_url, url, prices, marketplace)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "outbox_id": {"type": "integer", "minimum": 1},
                    "review_token": {"type": "string", "minLength": 1},
                    "decision": {"type": "string", "enum": list(_DECISIONS)},
                    "reason": {"type": "string", "maxLength": 500},
                    "new_text": {"type": "string", "maxLength": 4000},
                },
                "required": ["outbox_id", "review_token", "decision"],
                "additionalProperties": False,
            },
            handler=_h_submit_offer_review,
            is_read_only=False,
            safety_rules=(),
        ),
        ToolSpec(
            name="improve_message_copy",
            description=(
                "Devuelve contexto del item + un review_token para reescribir el "
                "caption. Sólo permite reescritura sobre items que pasen los "
                "gates duros."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "outbox_id": {"type": "integer", "minimum": 1},
                    "current_text": {"type": "string", "maxLength": 4000},
                },
                "required": ["outbox_id"],
                "additionalProperties": False,
            },
            handler=_h_improve_message_copy,
            is_read_only=False,
            safety_rules=("image_price_url", "ml_affiliate", "telegram_to_ml"),
        ),
        ToolSpec(
            name="submit_message_copy",
            description=(
                "Aplica el rewrite del caption usando el review_token de "
                "improve_message_copy. Solo modifica caption_override."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "outbox_id": {"type": "integer", "minimum": 1},
                    "review_token": {"type": "string", "minLength": 1},
                    "new_text": {"type": "string", "minLength": 1, "maxLength": 4000},
                },
                "required": ["outbox_id", "review_token", "new_text"],
                "additionalProperties": False,
            },
            handler=_h_submit_message_copy,
            is_read_only=False,
            safety_rules=(),
        ),
    ]


__all__ = ["build_quality_tools"]
