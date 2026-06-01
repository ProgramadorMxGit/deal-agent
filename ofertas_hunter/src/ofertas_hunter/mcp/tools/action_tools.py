"""Action tools del MCP server.

7 tools que mutan estado, todas con Hard_Rules aplicadas server-side:

- `discover_seeds(marketplace, limit)`: lanza un ciclo de discovery.
- `hunt_amazon(limit)`: procesa N URLs `kind=product` del frontier Amazon.
- `hunt_mercadolibre(limit)`: análogo para ML, respetando login pause.
- `dispatch_outbox(limit)`: ejecuta `dispatcher.tick()` hasta N veces.
- `revalidate_offer(outbox_id)`: corre el `PlaywrightRevalidator` sobre un item.
- `pause_marketplace(marketplace, reason, ttl_seconds)`: pausa un marketplace
  con TTL opcional.
- `unpause_marketplace(marketplace)`: limpia la pausa.

Las primeras 4 declaran `safety_rules=("schedule_authority", ...)` para que
durante hibernating/warmup no se ejecuten. Pause y unpause son control puro
y no requieren scheduler ACTIVE.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ...models import OutboxItem
from ..audit import sanitize
from ..context import BrowserUnavailableError
from ..safety import PauseInfo
from ..serializers import serialize_publish_outcome, serialize_revalidation
from . import ToolSpec


logger = logging.getLogger(__name__)


_MARKETPLACES = ("amazon", "mercadolibre")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summarize_outcome(o: Any) -> dict:
    return {
        "url": getattr(o, "url", None),
        "final_url": getattr(o, "final_url", None),
        "classification": getattr(o, "classification", None),
        "suggested_outbox_type": getattr(o, "suggested_outbox_type", None),
        "enqueued_outbox_id": getattr(o, "enqueued_outbox_id", None),
        "discarded_reason": getattr(o, "discarded_reason", None),
        # Captcha assessment (sólo Amazon hunt; ML lo deja en None).
        "captcha_confidence": getattr(o, "captcha_confidence", None),
        "captcha_should_pause_marketplace": getattr(
            o, "captcha_should_pause_marketplace", False
        ),
        "captcha_strong_signals": list(getattr(o, "captcha_strong_signals", ()) or ()),
        "captcha_visible_signals": list(getattr(o, "captcha_visible_signals", ()) or ()),
        "captcha_weak_signals": list(getattr(o, "captcha_weak_signals", ()) or ()),
        "captcha_debug_path": getattr(o, "captcha_debug_path", None),
    }


# ---------------------------------------------------------------------------
# pause / unpause
# ---------------------------------------------------------------------------


async def _h_pause_marketplace(ctx: Any, args: dict) -> dict:
    name = args["marketplace"]
    reason = args.get("reason", "manual")
    ttl_seconds = int(args.get("ttl_seconds", 0))
    until = (
        datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        if ttl_seconds > 0
        else None
    )
    info = PauseInfo(active=True, reason=reason, until=until)
    ctx.pause_state[name] = info

    # TTL automático: programa cleanup
    if ttl_seconds > 0:
        loop = asyncio.get_running_loop()
        loop.call_later(ttl_seconds, _clear_pause, ctx, name)

    # Auditoría dedicada (además del audit genérico del server)
    from ...runtime.events import emit_runtime_event

    emit_runtime_event(
        ctx.db,
        kind="mcp_marketplace_paused",
        severity="warning",
        payload={
            "marketplace": name,
            "reason": reason,
            "ttl_seconds": ttl_seconds,
            "until": info.until_iso,
        },
    )
    return {
        "success": True,
        "marketplace": name,
        "reason": reason,
        "until": info.until_iso,
    }


def _clear_pause(ctx: Any, name: str) -> None:
    info = ctx.pause_state.get(name)
    if info is not None:
        info.active = False


async def _h_unpause_marketplace(ctx: Any, args: dict) -> dict:
    name = args["marketplace"]
    info = ctx.pause_state.pop(name, None)
    was_paused = info is not None and info.active

    from ...runtime.events import emit_runtime_event

    emit_runtime_event(
        ctx.db,
        kind="mcp_marketplace_unpaused",
        severity="info",
        payload={"marketplace": name, "was_paused": was_paused},
    )
    return {"success": True, "marketplace": name, "was_paused": was_paused}


# ---------------------------------------------------------------------------
# discover_seeds
# ---------------------------------------------------------------------------


async def _h_discover_seeds(ctx: Any, args: dict) -> dict:
    marketplace = args["marketplace"]
    limit = int(args.get("limit", 4))
    async with ctx.lock_for(marketplace):
        try:
            if marketplace == "amazon":
                agent = await ctx.get_amazon_discovery()
            else:
                agent = await ctx.get_ml_discovery()
        except BrowserUnavailableError as exc:
            return {"error": "browser_unavailable", "detail": str(exc)}

        # Auto-bootstrap: si el frontier de este marketplace está vacío
        # (sin URLs para descubrir), siembra automáticamente desde
        # `config/seeds/<marketplace>.json`. Esto hace al bot
        # completamente autónomo: no depende de un setup manual previo.
        seeded = 0
        try:
            total_in_frontier = agent.frontier.count_pending(marketplace)
        except Exception:
            total_in_frontier = 0

        if total_in_frontier == 0:
            seeded = _auto_seed_from_json(agent, marketplace)
            if seeded:
                logger.info(
                    "discover_seeds(%s): frontier vacío, auto-sembrado %d URLs desde config/seeds/%s.json",
                    marketplace, seeded, marketplace,
                )

        agent.max_per_cycle = max(1, min(limit, 10))
        outcomes = await agent.discover_once()
        return {
            "success": True,
            "marketplace": marketplace,
            "auto_seeded": seeded,
            "processed": len(outcomes),
            "discovered": sum(o.discovered_count for o in outcomes),
            "persisted": sum(o.persisted_count for o in outcomes),
            "outcomes": [
                {
                    "url": o.url,
                    "kind": o.kind,
                    "discovered_count": o.discovered_count,
                    "persisted_count": o.persisted_count,
                    "discarded_reason": o.discarded_reason,
                }
                for o in outcomes
            ],
        }


def _auto_seed_from_json(agent: Any, marketplace: str) -> int:
    """Carga `config/seeds/<marketplace>.json` al frontier vía agent.seed_from_config.

    Defensivo: si el JSON no existe, está malformado o el agent no tiene
    `seed_from_config`, retorna 0 sin lanzar excepción.
    """
    import json as _json
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[4]
    seeds_path = repo_root / "config" / "seeds" / f"{marketplace}.json"
    if not seeds_path.exists():
        logger.warning("auto_seed: archivo no existe: %s", seeds_path)
        return 0
    try:
        urls = _json.loads(seeds_path.read_text(encoding="utf-8"))
        if not isinstance(urls, list):
            logger.warning("auto_seed: %s no contiene una lista", seeds_path)
            return 0
    except Exception:
        logger.exception("auto_seed: error leyendo %s", seeds_path)
        return 0

    if not hasattr(agent, "seed_from_config"):
        logger.warning("auto_seed: agent %s no soporta seed_from_config", type(agent).__name__)
        return 0
    try:
        return int(agent.seed_from_config(urls))
    except Exception:
        logger.exception("auto_seed: seed_from_config raised")
        return 0


# ---------------------------------------------------------------------------
# hunt_amazon / hunt_mercadolibre
# ---------------------------------------------------------------------------


async def _h_hunt_amazon(ctx: Any, args: dict) -> dict:
    limit = int(args.get("limit", 5))
    async with ctx.lock_for("amazon"):
        try:
            hunter = await ctx.get_amazon_hunter()
        except BrowserUnavailableError as exc:
            return {"error": "browser_unavailable", "detail": str(exc)}
        outcomes = await hunter.hunt_from_frontier(max_urls=limit)
        return {
            "success": True,
            "processed": len(outcomes),
            "enqueued": sum(1 for o in outcomes if o.enqueued_outbox_id is not None),
            "discarded": sum(1 for o in outcomes if o.discarded_reason),
            "outcomes": [_summarize_outcome(o) for o in outcomes],
        }


async def _h_hunt_mercadolibre(ctx: Any, args: dict) -> dict:
    limit = int(args.get("limit", 5))
    async with ctx.lock_for("mercadolibre"):
        try:
            hunter = await ctx.get_ml_hunter()
        except BrowserUnavailableError as exc:
            return {"error": "browser_unavailable", "detail": str(exc)}
        if getattr(hunter, "paused", False):
            return {"skipped": True, "reason": "ml_paused_for_login"}
        outcomes = await hunter.hunt_from_frontier(max_urls=limit)
        return {
            "success": True,
            "processed": len(outcomes),
            "enqueued": sum(1 for o in outcomes if o.enqueued_outbox_id is not None),
            "discarded": sum(1 for o in outcomes if o.discarded_reason),
            "outcomes": [_summarize_outcome(o) for o in outcomes],
        }


# ---------------------------------------------------------------------------
# process_telegram
# ---------------------------------------------------------------------------


async def _h_process_telegram(ctx: Any, args: dict) -> dict:
    settings = ctx.settings
    if not settings.telegram_enabled:
        return {"skipped": True, "reason": "telegram_disabled"}

    try:
        from ...agents.telegram_listener_agent import (
            MissingCredentialsError,
            TelegramListenerAgent,
            TelegramListenerConfig,
        )
        from ...telegram.channel_config import parse_channels
        from ...telegram.telethon_listener import TelethonAdapter, TelethonImportError
    except Exception as exc:
        return {"error": "telegram_import_failed", "detail": str(exc)}

    limit = int(args.get("limit", settings.telegram_backfill_limit_per_channel))
    budget = int(
        args.get("budget", settings.telegram_backfill_process_budget_per_channel)
    )
    config = TelegramListenerConfig(
        enabled=True,
        api_id=settings.resolved_telegram_api_id,
        api_hash=settings.resolved_telegram_api_hash,
        session_path=str(settings.telegram_session_path_resolved),
        target_channels=parse_channels(
            settings.telegram_target_channels or settings.telegram_channels
        ),
        backfill_limit_per_channel=limit,
        backfill_process_budget_per_channel=budget,
        ignore_mercadolibre_links=settings.telegram_ignore_mercadolibre_links,
        link_resolver_timeout_seconds=settings.telegram_link_resolver_timeout_seconds,
        link_resolver_max_redirects=settings.telegram_link_resolver_max_redirects,
        normal_offer_min_discount=settings.normal_offer_min_discount,
        start_from_now=settings.telegram_start_from_now,
    )
    try:
        adapter = TelethonAdapter(
            api_id=config.api_id,
            api_hash=config.api_hash,
            session_path=config.session_path,
        )
    except TelethonImportError as exc:
        return {"error": "telethon_missing", "detail": str(exc)}

    agent = TelegramListenerAgent(config=config, adapter=adapter, db_conn=ctx.db)
    try:
        try:
            agent.ensure_credentials()
        except MissingCredentialsError as exc:
            return {"error": "missing_credentials", "detail": str(exc)}
        outcomes = await agent.backfill_once()
    finally:
        await agent.aclose()

    return {
        "success": True,
        "processed": len(outcomes),
        "actionable": sum(1 for outcome in outcomes if outcome.candidate.is_actionable),
        "duplicates": sum(1 for outcome in outcomes if outcome.duplicate),
        "enqueued": sum(1 for outcome in outcomes if outcome.outbox_id is not None),
        "ignored": sum(1 for outcome in outcomes if outcome.candidate.is_ignored),
    }


# dispatch_outbox
# ---------------------------------------------------------------------------


async def _h_dispatch_outbox(ctx: Any, args: dict) -> dict:
    limit = int(args.get("limit", 1))
    results = []
    async with ctx.dispatcher_lock:
        dispatcher = ctx.get_dispatcher()
        for _ in range(max(1, min(limit, 10))):
            outcome = await dispatcher.tick()
            if outcome is None:
                break
            results.append(serialize_publish_outcome(outcome))
            # Refresh cooldown anchor
            if outcome.success and not outcome.skipped and not outcome.dry_run:
                ctx.record_normal_publication()
    return {"success": True, "ticks": len(results), "results": results}


# ---------------------------------------------------------------------------
# enrich_amazon_affiliates
# ---------------------------------------------------------------------------


async def _h_enrich_amazon_affiliates(ctx: Any, args: dict) -> dict:
    """Enriquece items Amazon `pending` sin affiliate_url usando SiteStripe.

    Reutiliza `AmazonAffiliateEnricher`. Emite eventos de runtime y nunca
    lanza: cualquier fallo se reporta como `success=False` para no congelar
    el orquestador.

    Serialización doble:
    - `ctx.lock_for("amazon")`: serializa con hunt/discover dentro del proceso.
    - `ProfileLock` (filelock): impide que CUALQUIER proceso (incl. CLI) abra
      un segundo `launch_persistent_context` sobre el mismo perfil. Si está
      ocupado, se salta y se reintenta el próximo ciclo (no abre Chromium).
    """
    from ...runtime.profile_lock import ProfileLock

    limit = int(args.get("limit", 5))

    async with ctx.lock_for("amazon"):
        profile_lock = ProfileLock(ctx.amazon_profile_lock_path)
        if not profile_lock.try_acquire():
            logger.warning("amazon_profile_lock_busy: enrich skipped (reintenta luego)")
            return {
                "success": False,
                "skipped": True,
                "reason": "amazon_profile_busy",
            }
        logger.info("amazon_profile_lock_acquired (enrich)")
        try:
            try:
                enricher = await ctx.get_amazon_affiliate_enricher()
            except BrowserUnavailableError as exc:
                return {"success": False, "error": "browser_unavailable", "detail": str(exc)}
            except Exception as exc:  # noqa: BLE001 - defensivo
                return {"success": False, "error": "enricher_unavailable", "detail": str(exc)}

            logger.info("affiliate_enrich_started limit=%d", limit)
            try:
                report = await enricher.run(limit=limit)
            except asyncio.CancelledError:
                # Timeout de dispatch_mcp: garantizamos cleanup del browser.
                logger.warning("affiliate_enrich_timeout: cancelado; limpiando browser")
                await _cleanup_amazon_browser(ctx)
                raise
            except Exception as exc:  # noqa: BLE001 - defensivo
                logger.warning("affiliate_enrich_failed: %s", exc)
                return {"success": False, "error": "enrich_failed", "detail": str(exc)}

            logger.info(
                "affiliate_enriched enriched=%d failed=%d candidates=%d",
                report.enriched, report.failed, report.total_candidates,
            )
            return {
                "success": True,
                "total_candidates": report.total_candidates,
                "enriched": report.enriched,
                "failed": report.failed,
                "skipped": report.skipped,
            }
        finally:
            profile_lock.release()
            logger.info("amazon_profile_lock_released (enrich)")


async def _cleanup_amazon_browser(ctx: Any) -> None:
    """Cierra el browser persistente de Amazon de forma idempotente.

    Se llama tras una cancelación/timeout para no dejar Chromium huérfano
    sobre el perfil. El próximo `get_amazon_browser()` reabrirá uno limpio.
    """
    logger.info("affiliate_cleanup_started")
    browser = getattr(ctx, "_amazon_browser", None)
    if browser is None:
        logger.info("affiliate_cleanup_done (no browser)")
        return
    try:
        await browser.aclose()
        logger.info("affiliate_cleanup_done")
    except Exception as exc:  # noqa: BLE001 - cleanup nunca es fatal
        logger.warning("affiliate_cleanup_failed: %s", exc)
    finally:
        # Idempotente: limpiar el singleton para forzar reapertura limpia.
        try:
            ctx._amazon_browser = None
        except Exception:  # pragma: no cover - defensivo
            pass


# ---------------------------------------------------------------------------
# revalidate_offer
# ---------------------------------------------------------------------------


async def _h_revalidate_offer(ctx: Any, args: dict) -> dict:
    outbox_id = int(args["outbox_id"])
    row = ctx.db.execute(
        "SELECT id, offer_id, type, enqueued_at, scheduled_for, attempts, last_attempt_at, "
        "state, message_payload_json FROM outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()
    if row is None:
        return {"error": "outbox_not_found", "outbox_id": outbox_id}

    import json as _json

    item = OutboxItem(
        offer_id=row["offer_id"],
        type=row["type"],
        message_payload=_json.loads(row["message_payload_json"] or "{}"),
        attempts=row["attempts"] or 0,
        state=row["state"],
        id=row["id"],
    )
    try:
        revalidator = await ctx.get_revalidator()
    except BrowserUnavailableError as exc:
        return {"error": "browser_unavailable", "detail": str(exc)}
    detail = await revalidator.revalidate_detailed(item)
    return {"success": True, "outbox_id": outbox_id, **serialize_revalidation(detail)}


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_action_tools(ctx: Any) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="pause_marketplace",
            description=(
                "Pausa hunts y discovery para un marketplace específico. Si "
                "ttl_seconds > 0, la pausa se limpia automáticamente."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "marketplace": {"type": "string", "enum": list(_MARKETPLACES)},
                    "reason": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "ttl_seconds": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 86400,
                        "default": 0,
                    },
                },
                "required": ["marketplace", "reason"],
                "additionalProperties": False,
            },
            handler=_h_pause_marketplace,
            is_read_only=False,
            safety_rules=(),
        ),
        ToolSpec(
            name="unpause_marketplace",
            description=(
                "Limpia la pausa de un marketplace. No-op si no estaba pausado."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "marketplace": {"type": "string", "enum": list(_MARKETPLACES)},
                },
                "required": ["marketplace"],
                "additionalProperties": False,
            },
            handler=_h_unpause_marketplace,
            is_read_only=False,
            safety_rules=(),
        ),
        ToolSpec(
            name="discover_seeds",
            description=(
                "Lanza un ciclo de discovery del marketplace indicado. Procesa "
                "URLs listing/category/deals del frontier y mete productos al "
                "frontier. Respeta scheduler y pausas."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "marketplace": {"type": "string", "enum": list(_MARKETPLACES)},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 4,
                    },
                },
                "required": ["marketplace"],
                "additionalProperties": False,
            },
            handler=_h_discover_seeds,
            is_read_only=False,
            safety_rules=("schedule_authority", "marketplace_paused"),
        ),
        ToolSpec(
            name="hunt_amazon",
            description=(
                "Procesa hasta `limit` URLs `kind=product` del frontier Amazon. "
                "Devuelve resumen de outcomes (enqueued / discarded). Respeta "
                "scheduler y pausa."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5,
                    },
                    "_marketplace": {
                        "type": "string",
                        "enum": ["amazon"],
                        "default": "amazon",
                    },
                },
                "additionalProperties": False,
            },
            handler=_h_hunt_amazon,
            is_read_only=False,
            safety_rules=("schedule_authority", "marketplace_paused"),
        ),
        ToolSpec(
            name="hunt_mercadolibre",
            description=(
                "Procesa hasta `limit` URLs `kind=product` del frontier ML. "
                "Devuelve {skipped: ml_paused_for_login} si el hunter está en "
                "modo login pause. Respeta scheduler y pausa."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5,
                    },
                    "_marketplace": {
                        "type": "string",
                        "enum": ["mercadolibre"],
                        "default": "mercadolibre",
                    },
                },
                "additionalProperties": False,
            },
            handler=_h_hunt_mercadolibre,
            is_read_only=False,
            safety_rules=("schedule_authority", "marketplace_paused"),
        ),
        ToolSpec(
            name="process_telegram",
            description=(
                "Procesa un backfill corto de canales Telegram configurados y "
                "encola ofertas accionables al outbox."
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
                    "budget": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 40,
                    },
                },
                "additionalProperties": False,
            },
            handler=_h_process_telegram,
            is_read_only=False,
            safety_rules=(),
        ),
        ToolSpec(
            name="dispatch_outbox",
            description=(
                "Ejecuta hasta `limit` ticks del OutboxDispatcher. Cada tick "
                "selecciona el próximo item publicable, lo formatea y llama a "
                "Evolution API (o dry-run si está activo). Respeta cooldown 5 min, "
                "ML afiliado obligatorio, modo seguro y scheduler."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 1,
                    },
                },
                "additionalProperties": False,
            },
            handler=_h_dispatch_outbox,
            is_read_only=False,
            safety_rules=("schedule_authority",),
        ),
        ToolSpec(
            name="enrich_amazon_affiliates",
            description=(
                "Enriquece items Amazon `pending` sin affiliate_url abriendo "
                "el producto con Playwright + SiteStripe y copiando el enlace "
                "corto de afiliado. Actualiza el payload del outbox. No publica."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 5,
                    },
                },
                "additionalProperties": False,
            },
            handler=_h_enrich_amazon_affiliates,
            is_read_only=False,
            safety_rules=(),
        ),
        ToolSpec(
            name="revalidate_offer",
            description=(
                "Revalida una oferta del outbox abriendo el producto con "
                "Playwright. Devuelve clasificación, confidence_label y razón "
                "de descarte si aplica. No publica."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "outbox_id": {"type": "integer", "minimum": 1},
                },
                "required": ["outbox_id"],
                "additionalProperties": False,
            },
            handler=_h_revalidate_offer,
            is_read_only=False,
            safety_rules=(),
        ),
    ]


__all__ = ["build_action_tools"]
