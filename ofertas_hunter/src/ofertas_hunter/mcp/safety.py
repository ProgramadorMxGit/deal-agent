"""Hard_Rules wrapper para el MCP server.

Se aplica entre la validación de schema y la ejecución del handler. Cada
`ToolSpec` declara qué reglas correr en `safety_rules`. La primera que
rechace devuelve un `SkipResult(skipped=True, reason=<token>)`. Si todas
pasan, se devuelve `OK`.

Las reglas son intocables: ningún argumento del cliente puede saltarlas.
Los tokens son estables y forman parte del contrato con el cliente:

- `hibernating`, `warmup`           → schedule_authority
- `paused`                          → marketplace_paused
- `cooldown_active`                 → cooldown_normal
- `missing_image_url`,
  `missing_current_price`,
  `missing_url`                     → image_price_url
- `missing_affiliate_url`           → ml_affiliate
- `telegram_to_ml_blocked`          → telegram_to_ml
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from ..models import OutboxType
from ..runtime.scheduler import ScheduleMode


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------


@dataclass
class SkipResult:
    skipped: bool
    reason: Optional[str] = None
    detail: Optional[dict] = field(default_factory=dict)


OK = SkipResult(skipped=False)


@dataclass
class PauseInfo:
    """Estado en memoria de un marketplace pausado por MCP."""

    active: bool
    reason: str
    until: Optional[datetime] = None  # None = sin TTL

    @property
    def until_iso(self) -> Optional[str]:
        if self.until is None:
            return None
        return self.until.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")


# Protocolo mínimo del contexto que las reglas necesitan. Lo definimos como
# tipo opaco para evitar import circular con `mcp/context.py`.
class _CtxProto:  # pragma: no cover - solo tipo
    db: sqlite3.Connection
    settings: Any
    scheduler: Any
    pause_state: dict[str, PauseInfo]
    last_normal_publication_at: Optional[datetime]


# Una regla recibe (ctx, args) y devuelve SkipResult.
RuleFn = Callable[[Any, dict], Awaitable[SkipResult]]


# ---------------------------------------------------------------------------
# Reglas
# ---------------------------------------------------------------------------


async def rule_schedule_authority(ctx: Any, args: dict) -> SkipResult:
    """Bloquea ejecución si el scheduler decide que NO es ACTIVE."""
    decision = ctx.scheduler.decide()
    if decision.mode == ScheduleMode.ACTIVE:
        return OK
    return SkipResult(
        skipped=True,
        reason=decision.mode.value,  # "hibernating" | "warmup"
        detail={
            "next_mode": decision.next_mode.value,
            "next_change_in_seconds": int(decision.next_change_in.total_seconds()),
        },
    )


def _infer_marketplace_from_args(args: dict) -> Optional[str]:
    """Deduce el marketplace de los argumentos de la tool.

    Las tools `hunt_amazon` / `hunt_mercadolibre` no envían `marketplace`,
    por eso el MCP server inyecta `_marketplace` antes de aplicar reglas.
    """
    if "marketplace" in args:
        return args.get("marketplace")
    if "_marketplace" in args:
        return args.get("_marketplace")
    return None


async def rule_marketplace_paused(ctx: Any, args: dict) -> SkipResult:
    """Si el marketplace está pausado, salta."""
    name = _infer_marketplace_from_args(args)
    if not name:
        return OK
    info = ctx.pause_state.get(name)
    if not info or not info.active:
        return OK
    # Si tiene TTL y ya venció, lo limpiamos y dejamos pasar.
    now = datetime.now(timezone.utc)
    if info.until is not None and info.until <= now:
        info.active = False
        return OK
    return SkipResult(
        skipped=True,
        reason="paused",
        detail={
            "marketplace": name,
            "reason_text": info.reason,
            "until": info.until_iso,
        },
    )


async def rule_cooldown_normal(ctx: Any, args: dict) -> SkipResult:
    """Bloquea publicación de tipo `normal` antes de que pasen 5 min."""
    last = getattr(ctx, "last_normal_publication_at", None)
    if last is None:
        return OK
    cooldown = int(getattr(ctx.settings, "whatsapp_cooldown_seconds", 300))
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    if elapsed >= cooldown:
        return OK
    return SkipResult(
        skipped=True,
        reason="cooldown_active",
        detail={"remaining_seconds": int(cooldown - elapsed)},
    )


async def rule_publishing_safe_mode(ctx: Any, args: dict) -> SkipResult:
    """Informativo: el publisher ya respeta los flags. Aquí no bloqueamos."""
    return OK


def _load_outbox_payload(
    db: sqlite3.Connection, outbox_id: int
) -> Optional[tuple[str, dict]]:
    """Lee `(type, payload)` del outbox o None si no existe."""
    row = db.execute(
        "SELECT type, message_payload_json FROM outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()
    if row is None:
        return None
    payload_json = (
        row["message_payload_json"]
        if isinstance(row, sqlite3.Row)
        else row[1]
    )
    item_type = row["type"] if isinstance(row, sqlite3.Row) else row[0]
    try:
        payload = json.loads(payload_json) if payload_json else {}
    except (TypeError, ValueError):
        payload = {}
    return item_type, payload


async def rule_image_price_url(ctx: Any, args: dict) -> SkipResult:
    """Para tools que apuntan a un OutboxItem específico, exige los gates duros."""
    outbox_id = args.get("outbox_id")
    if outbox_id is None:
        return OK
    loaded = _load_outbox_payload(ctx.db, int(outbox_id))
    if loaded is None:
        return OK  # el handler reportará "outbox_not_found"
    _item_type, payload = loaded
    if not payload.get("image_url"):
        return SkipResult(skipped=True, reason="missing_image_url")
    if payload.get("current_price") is None:
        return SkipResult(skipped=True, reason="missing_current_price")
    publishable_url = (
        payload.get("affiliate_url")
        or payload.get("url")
        or payload.get("canonical_url")
    )
    if not publishable_url:
        return SkipResult(skipped=True, reason="missing_url")
    return OK


async def rule_ml_affiliate(ctx: Any, args: dict) -> SkipResult:
    """Si está configurado, ML SIN affiliate_url no puede publicarse."""
    if not getattr(
        ctx.settings, "mercadolibre_affiliate_required_for_publish", False
    ):
        return OK
    outbox_id = args.get("outbox_id")
    if outbox_id is None:
        return OK
    loaded = _load_outbox_payload(ctx.db, int(outbox_id))
    if loaded is None:
        return OK
    _item_type, payload = loaded
    if (payload.get("marketplace") or "").lower() != "mercadolibre":
        return OK
    if not payload.get("affiliate_url"):
        return SkipResult(skipped=True, reason="missing_affiliate_url")
    return OK


async def rule_telegram_to_ml(ctx: Any, args: dict) -> SkipResult:
    """ML cuya source es Telegram nunca se publica."""
    outbox_id = args.get("outbox_id")
    if outbox_id is None:
        return OK
    loaded = _load_outbox_payload(ctx.db, int(outbox_id))
    if loaded is None:
        return OK
    _item_type, payload = loaded
    is_ml = (payload.get("marketplace") or "").lower() == "mercadolibre"
    src = (payload.get("source") or "").lower()
    if is_ml and src == "telegram":
        return SkipResult(skipped=True, reason="telegram_to_ml_blocked")
    return OK


# ---------------------------------------------------------------------------
# Registry y dispatch
# ---------------------------------------------------------------------------


_RULES: dict[str, RuleFn] = {
    "schedule_authority": rule_schedule_authority,
    "marketplace_paused": rule_marketplace_paused,
    "cooldown_normal": rule_cooldown_normal,
    "publishing_safe_mode": rule_publishing_safe_mode,
    "image_price_url": rule_image_price_url,
    "ml_affiliate": rule_ml_affiliate,
    "telegram_to_ml": rule_telegram_to_ml,
}


async def apply_hard_rules(
    ctx: Any, safety_rules: tuple[str, ...], args: dict
) -> SkipResult:
    """Aplica las reglas declaradas para una tool. Devuelve la primera que rechaza."""
    for rule_name in safety_rules:
        rule = _RULES.get(rule_name)
        if rule is None:
            continue  # regla desconocida es no-op (defensivo)
        result = await rule(ctx, args)
        if result.skipped:
            return result
    return OK


def list_rule_names() -> list[str]:
    """Útil para tests y diagnóstico."""
    return list(_RULES.keys())


__all__ = [
    "OK",
    "PauseInfo",
    "SkipResult",
    "apply_hard_rules",
    "list_rule_names",
    "rule_cooldown_normal",
    "rule_image_price_url",
    "rule_marketplace_paused",
    "rule_ml_affiliate",
    "rule_publishing_safe_mode",
    "rule_schedule_authority",
    "rule_telegram_to_ml",
]
