"""Read tools del MCP server.

Cinco tools sin efectos secundarios:

- `get_status`: estado runtime, flags de publicación, agentes registrados,
  marketplaces pausados, lock holder.
- `get_schedule_mode`: decisión actual del scheduler con `next_change_in_seconds`.
- `get_outbox(limit, type_filter)`: items recientes del outbox.
- `get_recent_events(limit, severity)`: runtime_events recientes.
- `get_frontier_stats(marketplace)`: conteo del frontier por estado/kind.

Todas con `is_read_only=True` y `safety_rules=()`. NUNCA escriben en DB
(salvo los 2 eventos `mcp_tool_called` que el dispatcher emite por su cuenta).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..lockfile import FileLock
from ..serializers import (
    serialize_outbox_row,
    serialize_runtime_event_row,
    serialize_schedule_decision,
)
from . import ToolSpec


# ---------------------------------------------------------------------------
# Constantes / schemas
# ---------------------------------------------------------------------------


_SEVERITIES = ("info", "warning", "error", "critical", "any")
_OUTBOX_TYPES = ("normal", "price_error", "possible_pe", "any")
_MARKETPLACES = ("amazon", "mercadolibre")

AGENTS_REGISTERED_DEFAULT = (
    "amazon_hunter",
    "mercadolibre_hunter",
    "outbox_dispatcher",
    "telegram_listener",
    "maintenance",
)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _h_get_status(ctx: Any, args: dict) -> dict:
    decision = ctx.scheduler.decide()
    paused = {
        name: info.until_iso
        for name, info in ctx.pause_state.items()
        if info.active
    }
    settings = ctx.settings
    enabled_agents = []
    if settings.amazon_enabled:
        enabled_agents.append("amazon_hunter")
    if settings.mercadolibre_enabled:
        enabled_agents.append("mercadolibre_hunter")
    if settings.telegram_enabled:
        enabled_agents.append("telegram_listener")
    enabled_agents.append("outbox_dispatcher")
    enabled_agents.append("maintenance")

    # Outbox summary
    outbox_summary = {}
    for row in ctx.db.execute(
        "SELECT type, state, COUNT(*) AS n FROM outbox GROUP BY type, state"
    ):
        d = dict(row)
        outbox_summary.setdefault(d["type"], {})[d["state"]] = d["n"]

    return {
        "schedule_mode": decision.mode.value,
        "next_mode": decision.next_mode.value,
        "next_change_in_seconds": int(decision.next_change_in.total_seconds()),
        "local_time": decision.local_time.isoformat(timespec="seconds"),
        "publishing_enabled": settings.publishing_enabled,
        "publishing_dry_run": settings.publishing_dry_run,
        "amazon_enabled": settings.amazon_enabled,
        "mercadolibre_enabled": settings.mercadolibre_enabled,
        "telegram_enabled": settings.telegram_enabled,
        "ml_affiliate_required": settings.mercadolibre_affiliate_required_for_publish,
        "agents_registered": enabled_agents,
        "marketplace_paused": paused,
        "outbox_summary": outbox_summary,
        "last_normal_publication_at": (
            ctx.last_normal_publication_at.isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            )
            if ctx.last_normal_publication_at is not None
            else None
        ),
    }


async def _h_get_schedule_mode(ctx: Any, args: dict) -> dict:
    return serialize_schedule_decision(ctx.scheduler.decide())


async def _h_get_outbox(ctx: Any, args: dict) -> dict:
    limit = int(args.get("limit", 20))
    type_filter = args.get("type_filter", "any")
    if type_filter == "any":
        rows = ctx.db.execute(
            "SELECT id, offer_id, type, enqueued_at, scheduled_for, attempts, "
            "last_attempt_at, state, message_payload_json FROM outbox "
            "ORDER BY enqueued_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = ctx.db.execute(
            "SELECT id, offer_id, type, enqueued_at, scheduled_for, attempts, "
            "last_attempt_at, state, message_payload_json FROM outbox "
            "WHERE type = ? ORDER BY enqueued_at DESC LIMIT ?",
            (type_filter, limit),
        ).fetchall()
    return {
        "data": [serialize_outbox_row(r) for r in rows],
        "meta": {"limit": limit, "type_filter": type_filter, "count": len(rows)},
    }


async def _h_get_recent_events(ctx: Any, args: dict) -> dict:
    limit = int(args.get("limit", 50))
    severity = args.get("severity", "any")
    if severity == "any":
        rows = ctx.db.execute(
            "SELECT id, kind, severity, payload_json, created_at, acknowledged_at "
            "FROM runtime_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = ctx.db.execute(
            "SELECT id, kind, severity, payload_json, created_at, acknowledged_at "
            "FROM runtime_events WHERE severity = ? ORDER BY id DESC LIMIT ?",
            (severity, limit),
        ).fetchall()
    return {
        "data": [serialize_runtime_event_row(r) for r in rows],
        "meta": {"limit": limit, "severity": severity, "count": len(rows)},
    }


async def _h_get_frontier_stats(ctx: Any, args: dict) -> dict:
    marketplace = args["marketplace"]
    rows = ctx.db.execute(
        "SELECT url_type, COUNT(*) AS n FROM frontier "
        "WHERE marketplace = ? GROUP BY url_type",
        (marketplace,),
    ).fetchall()
    by_url_type: dict[str, int] = {}
    total = 0
    for r in rows:
        d = dict(r)
        by_url_type[d["url_type"]] = d["n"]
        total += d["n"]
    return {"marketplace": marketplace, "by_url_type": by_url_type, "total": total}


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_read_tools(ctx: Any) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="get_status",
            description=(
                "Devuelve el estado runtime del bot: modo del scheduler, flags de "
                "publicación (enabled, dry_run), agentes habilitados, marketplaces "
                "pausados y resumen del outbox. No modifica ningún estado."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=_h_get_status,
            is_read_only=True,
        ),
        ToolSpec(
            name="get_schedule_mode",
            description=(
                "Modo actual del OperatingScheduler (active | hibernating | warmup) "
                "con tiempo restante hasta el próximo cambio."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=_h_get_schedule_mode,
            is_read_only=True,
        ),
        ToolSpec(
            name="get_outbox",
            description=(
                "Items recientes del outbox, ordenados por enqueued_at desc. "
                "Filtrable por tipo (normal | price_error | possible_pe | any). "
                "No publica ni modifica items."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                    },
                    "type_filter": {
                        "type": "string",
                        "enum": list(_OUTBOX_TYPES),
                        "default": "any",
                    },
                },
                "additionalProperties": False,
            },
            handler=_h_get_outbox,
            is_read_only=True,
        ),
        ToolSpec(
            name="get_recent_events",
            description=(
                "runtime_events recientes para diagnóstico. Filtrable por severity "
                "(info | warning | error | critical | any)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 50,
                    },
                    "severity": {
                        "type": "string",
                        "enum": list(_SEVERITIES),
                        "default": "any",
                    },
                },
                "additionalProperties": False,
            },
            handler=_h_get_recent_events,
            is_read_only=True,
        ),
        ToolSpec(
            name="get_frontier_stats",
            description=(
                "Conteo del frontier de descubrimiento agrupado por kind y status. "
                "Útil para decidir si lanzar discovery o hunt."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "marketplace": {
                        "type": "string",
                        "enum": list(_MARKETPLACES),
                    },
                },
                "required": ["marketplace"],
                "additionalProperties": False,
            },
            handler=_h_get_frontier_stats,
            is_read_only=True,
        ),
    ]


__all__ = ["AGENTS_REGISTERED_DEFAULT", "build_read_tools"]
