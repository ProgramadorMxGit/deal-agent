"""Orchestrator: arma el grafo completo de agentes según `.env`.

Responsabilidades:

- Construir y registrar al `RuntimeWatchdog` los agentes que estén
  habilitados (`AMAZON_ENABLED`, `MERCADOLIBRE_ENABLED`, `TELEGRAM_ENABLED`,
  publicación siempre activa pero respeta `PUBLISHING_ENABLED`).
- Cada agente es una **factory** `(handle, registry) -> Awaitable[None]` que:
  1. Llama a `registry.heartbeat(handle)` periódicamente.
  2. Hace su unidad de trabajo (un tick del dispatcher, un backfill de
     Telegram, un ciclo de hunt sobre N seeds, …).
  3. Espera el `loop_interval` configurado y repite.
  4. Si `--once` se pasa al `run`, sale tras el primer ciclo.
- Si una dependencia opcional falla (Playwright no instalado, cookies
  ausentes, etc.), el agente queda registrado pero **dormita**: emite un
  `runtime_event(severity=warning, kind=agent_skipped)` y reintenta
  periódicamente. No crashea ni arrastra al watchdog.

Las factories están desacopladas para que los tests usen
`AgentRegistrar` con stubs en vez del builder real.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .config import Settings
from .runtime.events import emit_runtime_event
from .runtime.heartbeat import AgentRunHandle, AgentRunRegistry
from .runtime.watchdog import RuntimeWatchdog, WatchdogConfig


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tipos y configuración
# ---------------------------------------------------------------------------


# Una factory recibe (handle, registry) y devuelve coroutine de larga duración.
AgentFactoryFn = Callable[[AgentRunHandle, AgentRunRegistry], Awaitable[None]]


@dataclass
class OrchestratorConfig:
    """Parámetros operativos. Pueden venir de `.env` o ser inyectados."""

    once: bool = False
    dispatcher_loop_interval: float = 5.0
    amazon_loop_interval: float = 600.0
    mercadolibre_loop_interval: float = 600.0
    telegram_loop_interval: float = 30.0
    maintenance_loop_interval: float = 6 * 3600.0
    watchdog_poll_interval: float = 30.0
    watchdog_stale_after: float = 120.0
    amazon_seeds: list[str] = field(default_factory=list)
    amazon_hunt_limit: int = 5
    mercadolibre_seeds: list[str] = field(default_factory=list)
    mercadolibre_hunt_limit: int = 5
    # Scheduler nocturno
    schedule_enabled: bool = True
    schedule_timezone: str = "America/Mexico_City"
    hibernate_start: str = "23:30"
    hibernate_end: str = "06:30"
    warmup_start: str = "06:30"
    active_start: str = "07:00"
    # En warmup, los hunters trabajan más rápido para llenar el outbox.
    warmup_loop_interval: float = 90.0
    # En hibernación, los loops despiertan periódicamente sólo para
    # comprobar si el modo cambió. No fetchean.
    hibernation_check_interval: float = 60.0


# ---------------------------------------------------------------------------
# Resultado del registro
# ---------------------------------------------------------------------------


@dataclass
class RegistrationReport:
    registered: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)


# ---------------------------------------------------------------------------
# Constructor de factories (extensible y testeable)
# ---------------------------------------------------------------------------


class AgentFactoryBuilder:
    """Genera factories listas para `RuntimeWatchdog.register()`.

    Esta clase no toca `.env` directamente: recibe `Settings` ya resuelta.
    Las factories reales (que abren Playwright/Telethon) viven aquí, pero
    los tests pueden subclasear y devolver factories triviales.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        config: OrchestratorConfig,
    ) -> None:
        self.db = conn
        self.settings = settings
        self.config = config
        # Único scheduler compartido por todos los agentes y el dispatcher.
        from .runtime.scheduler import OperatingScheduler, ScheduleConfig

        sched_config = ScheduleConfig.from_env(
            enabled=config.schedule_enabled,
            timezone_name=config.schedule_timezone,
            hibernate_start=config.hibernate_start,
            hibernate_end=config.hibernate_end,
            warmup_start=config.warmup_start,
            active_start=config.active_start,
        )
        self.scheduler = OperatingScheduler(sched_config)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit_skip(self, agent_name: str, reason: str) -> None:
        emit_runtime_event(
            self.db,
            kind="agent_skipped",
            severity="warning",
            payload={"agent": agent_name, "reason": reason},
        )
        logger.warning("agent %s no se registra: %s", agent_name, reason)

    @staticmethod
    async def _loop(
        *,
        handle: AgentRunHandle,
        registry: AgentRunRegistry,
        once: bool,
        interval: float,
        work: Callable[[], Awaitable[None]],
        scheduler: Optional["object"] = None,
        warmup_interval: Optional[float] = None,
        hibernation_check_interval: float = 60.0,
        respect_schedule: bool = True,
    ) -> None:
        """Loop genérico con heartbeat + modo según scheduler.

        Si `scheduler` está y `respect_schedule=True`, el loop:
        - en `hibernating` → no llama a `work()`, sólo heartbeat cada
          `hibernation_check_interval`.
        - en `warmup` → usa `warmup_interval` (más corto) en lugar de
          `interval`.
        - en `active` → usa `interval`.
        """
        from .runtime.scheduler import ScheduleMode

        registry.heartbeat(handle)
        try:
            while True:
                effective_interval = interval
                run_work = True

                if scheduler is not None and respect_schedule:
                    decision = scheduler.decide()
                    if decision.mode == ScheduleMode.HIBERNATING:
                        run_work = False
                        effective_interval = hibernation_check_interval
                    elif decision.mode == ScheduleMode.WARMUP and warmup_interval:
                        effective_interval = warmup_interval

                if run_work:
                    try:
                        await work()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("agent_loop work raised")

                registry.heartbeat(handle)
                if once:
                    return
                try:
                    await asyncio.sleep(effective_interval)
                except asyncio.CancelledError:
                    raise
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # Factories concretas
    # ------------------------------------------------------------------

    def make_dispatcher_factory(self) -> AgentFactoryFn:
        """Factory para el outbox dispatcher.

        Si la config Evolution está incompleta, sigue corriendo: los items
        se quedan pendientes. Si `PUBLISHING_ENABLED=false`, el publisher
        retorna `skipped=True` (ver `WhatsAppPublisher`).
        """
        from .dispatching.cooldown import CooldownPolicy
        from .dispatching.dispatcher import (
            OutboxDispatcher,
            make_sqlite_published_recorder,
        )
        from .dispatching.outbox import OutboxConfig, SqliteOutbox
        from .publishing.evolution_client import EvolutionClient
        from .publishing.whatsapp_publisher import WhatsAppPublisher

        s = self.settings
        cfg = self.config

        async def factory(handle: AgentRunHandle, registry: AgentRunRegistry) -> None:
            client = EvolutionClient(
                base_url=s.evolution_base_url,
                api_key=s.evolution_api_key,
                instance=s.evolution_instance,
                dry_run=s.publishing_dry_run,
            )
            publisher = WhatsAppPublisher(
                client=client,
                target_group_id=s.whatsapp_group,
                enabled=s.publishing_enabled,
                mercadolibre_affiliate_required=s.mercadolibre_affiliate_required_for_publish,
            )
            outbox = SqliteOutbox(
                self.db,
                OutboxConfig(
                    revalidate_age_seconds=s.whatsapp_outbox_revalidate_age_seconds,
                    cooldown=CooldownPolicy(cooldown_seconds=s.whatsapp_cooldown_seconds),
                ),
            )
            dispatcher = OutboxDispatcher(
                outbox=outbox,
                publisher=publisher,
                published_recorder=make_sqlite_published_recorder(self.db),
                idle_sleep_seconds=cfg.dispatcher_loop_interval,
                scheduler=self.scheduler,
            )

            async def work() -> None:
                await dispatcher.tick()

            try:
                await self._loop(
                    handle=handle,
                    registry=registry,
                    once=cfg.once,
                    interval=cfg.dispatcher_loop_interval,
                    work=work,
                )
            finally:
                await client.aclose()

        return factory

    def make_amazon_hunter_factory(self) -> Optional[AgentFactoryFn]:
        if not self.settings.amazon_enabled:
            self._emit_skip("amazon_hunter", "amazon_disabled")
            return None

        from .agents.amazon_hunter_agent import AmazonHunterAgent
        from .agents.discovery_agent import DiscoveryAgent
        from .browser.browser_context import BrowserConfig
        from .browser.playwright_worker import (
            PlaywrightBrowserWorker,
            PlaywrightImportError,
        )

        seeds = list(self.config.amazon_seeds)
        cfg = self.config
        s = self.settings

        async def factory(handle: AgentRunHandle, registry: AgentRunRegistry) -> None:
            try:
                browser = PlaywrightBrowserWorker(
                    BrowserConfig(
                        headless=s.amazon_headless,
                        user_data_dir=s.amazon_user_data_dir,
                        warmup_amazon_homepage=s.amazon_warmup_homepage,
                        delay_between_requests_ms=(
                            s.amazon_delay_between_pages_ms_min,
                            s.amazon_delay_between_pages_ms_max,
                        ),
                    )
                )
            except PlaywrightImportError as exc:
                emit_runtime_event(
                    self.db,
                    kind="agent_skipped",
                    severity="warning",
                    payload={"agent": "amazon_hunter", "reason": f"playwright_missing: {exc}"},
                )
                async def idle() -> None:
                    await asyncio.sleep(60)

                await self._loop(
                    handle=handle,
                    registry=registry,
                    once=cfg.once,
                    interval=cfg.amazon_loop_interval,
                    work=idle,
                )
                return

            hunter = AmazonHunterAgent(browser=browser, db_conn=self.db)
            discovery = DiscoveryAgent(
                browser=browser,
                db_conn=self.db,
                marketplace="amazon",
                max_per_cycle=2,
            )
            # Sembrar listings/categorías al frontier (idempotente)
            seeded = discovery.seed_from_config(seeds)
            if seeded:
                logger.info("amazon discovery: %d seeds añadidas al frontier", seeded)

            async def work() -> None:
                # 1) Descubrir productos en listings/categorías → frontier
                await discovery.discover_once()
                # 2) Procesar productos del frontier
                outcomes = await hunter.hunt_from_frontier(
                    max_urls=cfg.amazon_hunt_limit
                )
                if outcomes:
                    logger.info(
                        "amazon hunt: procesados=%d encolados=%d descartados=%d",
                        len(outcomes),
                        sum(1 for o in outcomes if o.enqueued_outbox_id is not None),
                        sum(1 for o in outcomes if o.discarded_reason),
                    )

            try:
                async with browser:
                    await self._loop(
                        handle=handle,
                        registry=registry,
                        once=cfg.once,
                        interval=cfg.amazon_loop_interval,
                        work=work,
                        scheduler=self.scheduler,
                        warmup_interval=cfg.warmup_loop_interval,
                        hibernation_check_interval=cfg.hibernation_check_interval,
                    )
            finally:
                await hunter.aclose()

        return factory

    def make_mercadolibre_hunter_factory(self) -> Optional[AgentFactoryFn]:
        if not self.settings.mercadolibre_enabled:
            self._emit_skip("mercadolibre_hunter", "mercadolibre_disabled")
            return None

        from .agents.discovery_agent import DiscoveryAgent
        from .agents.mercadolibre_hunter_agent import MercadoLibreHunterAgent
        from .browser.browser_context import BrowserConfig
        from .browser.playwright_worker import (
            PlaywrightBrowserWorker,
            PlaywrightImportError,
        )
        from .marketplaces.mercadolibre_affiliate import (
            PlaywrightAffiliateExtractor,
        )
        from .session.mercadolibre_session import MercadoLibreSession

        seeds = list(self.config.mercadolibre_seeds)
        cfg = self.config
        s = self.settings

        async def factory(handle: AgentRunHandle, registry: AgentRunRegistry) -> None:
            try:
                browser = PlaywrightBrowserWorker(
                    BrowserConfig(
                        headless=s.mercadolibre_headless,
                        user_data_dir=s.mercadolibre_user_data_dir,
                    )
                )
            except PlaywrightImportError as exc:
                emit_runtime_event(
                    self.db,
                    kind="agent_skipped",
                    severity="warning",
                    payload={
                        "agent": "mercadolibre_hunter",
                        "reason": f"playwright_missing: {exc}",
                    },
                )

                async def idle() -> None:
                    await asyncio.sleep(60)

                await self._loop(
                    handle=handle,
                    registry=registry,
                    once=cfg.once,
                    interval=cfg.mercadolibre_loop_interval,
                    work=idle,
                )
                return

            await browser._ensure_started()  # noqa: SLF001 — necesitamos el context

            session = MercadoLibreSession.from_settings(
                cookies_path=s.mercadolibre_cookies_path,
                fallback_path=s.mercadolibre_cookies_fallback_path,
            )
            cookies, health = session.load()
            if cookies and browser._context is not None:  # noqa: SLF001
                # Normalizar al formato que acepta Playwright (replicando sanitize_cookie del legacy)
                def _norm(c: dict) -> dict:
                    n = {
                        "name": c.get("name", ""),
                        "value": c.get("value", ""),
                        "domain": c.get("domain", ""),
                        "path": c.get("path", "/"),
                        "httpOnly": bool(c.get("httpOnly", False)),
                        "secure": bool(c.get("secure", False)),
                    }
                    _sm = {"no_restriction": "None", "lax": "Lax", "strict": "Strict", "none": "None", "unspecified": "None"}
                    ss = c.get("sameSite", "")
                    n["sameSite"] = "None" if not ss else (_sm.get(ss.lower(), ss.capitalize()) if ss not in ("Strict", "Lax", "None") else ss)
                    exp = c.get("expires") or c.get("expirationDate") or c.get("expiry")
                    if c.get("session") is True:
                        exp = None
                    if isinstance(exp, (int, float)) and exp > 0:
                        n["expires"] = float(exp)
                    return n
                normalized = [_norm(c) for c in cookies if c.get("name") and "value" in c]
                await browser._context.add_cookies(normalized)  # noqa: SLF001
            if health.is_missing or health.is_empty:
                emit_runtime_event(
                    self.db,
                    kind="cookie_expiry",
                    severity="warning",
                    payload={"agent": "mercadolibre_hunter", "reason": "cookies_missing_or_empty"},
                )

            extractor = PlaywrightAffiliateExtractor(browser._context)  # noqa: SLF001
            hunter = MercadoLibreHunterAgent(
                browser=browser,
                db_conn=self.db,
                affiliate_extractor=extractor,
                affiliate_required_for_publish=s.mercadolibre_affiliate_required_for_publish,
            )
            discovery = DiscoveryAgent(
                browser=browser,
                db_conn=self.db,
                marketplace="mercadolibre",
                max_per_cycle=2,
            )
            seeded = discovery.seed_from_config(seeds)
            if seeded:
                logger.info("ml discovery: %d seeds añadidas al frontier", seeded)

            async def work() -> None:
                if hunter.paused:
                    emit_runtime_event(
                        self.db,
                        kind="agent_paused",
                        severity="warning",
                        payload={"agent": "mercadolibre_hunter", "reason": "paused_for_login"},
                    )
                    return
                await discovery.discover_once()
                outcomes = await hunter.hunt_from_frontier(
                    max_urls=cfg.mercadolibre_hunt_limit
                )
                if outcomes:
                    logger.info(
                        "ml hunt: procesados=%d encolados=%d descartados=%d",
                        len(outcomes),
                        sum(1 for o in outcomes if o.enqueued_outbox_id is not None),
                        sum(1 for o in outcomes if o.discarded_reason),
                    )

            try:
                await self._loop(
                    handle=handle,
                    registry=registry,
                    once=cfg.once,
                    interval=cfg.mercadolibre_loop_interval,
                    work=work,
                    scheduler=self.scheduler,
                    warmup_interval=cfg.warmup_loop_interval,
                    hibernation_check_interval=cfg.hibernation_check_interval,
                )
            finally:
                await hunter.aclose()

        return factory

    def make_telegram_listener_factory(self) -> Optional[AgentFactoryFn]:
        if not self.settings.telegram_enabled:
            self._emit_skip("telegram_listener", "telegram_disabled")
            return None

        from .agents.telegram_listener_agent import (
            MissingCredentialsError,
            TelegramListenerAgent,
            TelegramListenerConfig,
        )
        from .telegram.channel_config import parse_channels

        s = self.settings
        cfg = self.config

        async def factory(handle: AgentRunHandle, registry: AgentRunRegistry) -> None:
            # Lazy import: si Telethon no está instalado, dormita.
            try:
                from .telegram.telethon_listener import (
                    TelethonAdapter,
                    TelethonImportError,
                )
            except Exception as exc:  # pragma: no cover
                emit_runtime_event(
                    self.db,
                    kind="agent_skipped",
                    severity="warning",
                    payload={
                        "agent": "telegram_listener",
                        "reason": f"telethon_import_failed: {exc}",
                    },
                )

                async def idle() -> None:
                    await asyncio.sleep(60)

                await self._loop(
                    handle=handle,
                    registry=registry,
                    once=cfg.once,
                    interval=cfg.telegram_loop_interval,
                    work=idle,
                )
                return

            entries = parse_channels(s.telegram_target_channels or s.telegram_channels)
            tg_config = TelegramListenerConfig(
                enabled=True,
                api_id=s.resolved_telegram_api_id,
                api_hash=s.resolved_telegram_api_hash,
                session_path=str(s.telegram_session_path_resolved),
                target_channels=entries,
                backfill_limit_per_channel=s.telegram_backfill_limit_per_channel,
                backfill_process_budget_per_channel=s.telegram_backfill_process_budget_per_channel,
                ignore_mercadolibre_links=s.telegram_ignore_mercadolibre_links,
                link_resolver_timeout_seconds=s.telegram_link_resolver_timeout_seconds,
                link_resolver_max_redirects=s.telegram_link_resolver_max_redirects,
            )

            try:
                adapter = TelethonAdapter(
                    api_id=tg_config.api_id,
                    api_hash=tg_config.api_hash,
                    session_path=tg_config.session_path,
                )
            except TelethonImportError as exc:
                emit_runtime_event(
                    self.db,
                    kind="agent_skipped",
                    severity="warning",
                    payload={
                        "agent": "telegram_listener",
                        "reason": f"telethon_missing: {exc}",
                    },
                )

                async def idle() -> None:
                    await asyncio.sleep(60)

                await self._loop(
                    handle=handle,
                    registry=registry,
                    once=cfg.once,
                    interval=cfg.telegram_loop_interval,
                    work=idle,
                )
                return

            agent = TelegramListenerAgent(
                config=tg_config, adapter=adapter, db_conn=self.db
            )

            async def work() -> None:
                try:
                    agent.ensure_credentials()
                except MissingCredentialsError as exc:
                    emit_runtime_event(
                        self.db,
                        kind="missing_credentials",
                        severity="error",
                        payload={"agent": "telegram_listener", "reason": str(exc)},
                    )
                    return
                # Un backfill corto por ciclo. La live-listening se delega a
                # listen_once() solo si el operador lo activa manualmente.
                await agent.backfill_once()

            try:
                await self._loop(
                    handle=handle,
                    registry=registry,
                    once=cfg.once,
                    interval=cfg.telegram_loop_interval,
                    work=work,
                )
            finally:
                await agent.aclose()

        return factory

    def make_maintenance_factory(self) -> AgentFactoryFn:
        from .memory.compressor import MemoryCompressor

        cfg = self.config

        async def factory(handle: AgentRunHandle, registry: AgentRunRegistry) -> None:
            compressor = MemoryCompressor(self.db)

            async def work() -> None:
                report = compressor.run()
                logger.info(
                    "memory_compactation: dom=%d events=%d disc=%d runs=%d summaries=%s",
                    report.deleted_dom_snapshots,
                    report.deleted_runtime_events,
                    report.deleted_discarded_candidates,
                    report.deleted_agent_runs,
                    report.summary_kinds,
                )

            await self._loop(
                handle=handle,
                registry=registry,
                once=cfg.once,
                interval=cfg.maintenance_loop_interval,
                work=work,
            )

        return factory


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """Wire-up entre `Settings`, `RuntimeWatchdog` y las factories."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        config: OrchestratorConfig,
        *,
        builder: Optional[AgentFactoryBuilder] = None,
        watchdog_config: Optional[WatchdogConfig] = None,
    ) -> None:
        self.db = conn
        self.settings = settings
        self.config = config
        self.builder = builder or AgentFactoryBuilder(conn, settings, config)
        wd_config = watchdog_config or WatchdogConfig(
            poll_interval_seconds=config.watchdog_poll_interval,
            stale_after_seconds=config.watchdog_stale_after,
        )
        self.watchdog = RuntimeWatchdog(conn, config=wd_config)

    # ------------------------------------------------------------------
    # Registro
    # ------------------------------------------------------------------

    def register_agents(self) -> RegistrationReport:
        report = RegistrationReport()

        # Maintenance siempre activo (low-cost).
        self.watchdog.register("maintenance", self.builder.make_maintenance_factory())
        report.registered.append("maintenance")

        # Dispatcher siempre se registra (drena outbox).
        self.watchdog.register(
            "outbox_dispatcher", self.builder.make_dispatcher_factory()
        )
        report.registered.append("outbox_dispatcher")

        # Hunters según flags
        amazon = self.builder.make_amazon_hunter_factory()
        if amazon is not None:
            self.watchdog.register("amazon_hunter", amazon)
            report.registered.append("amazon_hunter")
        else:
            report.skipped.append(("amazon_hunter", "disabled_or_missing_config"))

        ml = self.builder.make_mercadolibre_hunter_factory()
        if ml is not None:
            self.watchdog.register("mercadolibre_hunter", ml)
            report.registered.append("mercadolibre_hunter")
        else:
            report.skipped.append(("mercadolibre_hunter", "disabled_or_missing_config"))

        telegram = self.builder.make_telegram_listener_factory()
        if telegram is not None:
            self.watchdog.register("telegram_listener", telegram)
            report.registered.append("telegram_listener")
        else:
            report.skipped.append(("telegram_listener", "disabled_or_missing_config"))

        return report

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        report = self.register_agents()
        emit_runtime_event(
            self.db,
            kind="orchestrator_starting",
            severity="info",
            payload={
                "registered": report.registered,
                "skipped": [{"agent": a, "reason": r} for a, r in report.skipped],
                "once": self.config.once,
            },
        )

        await self.watchdog.start_all()

        if self.config.once:
            # Modo --once: esperamos a que cada task termine y salimos.
            await self._await_all()
            await self.watchdog.shutdown_all()
            return

        # Modo continuo: el watchdog corre forever.
        try:
            await self.watchdog.run_forever()
        except asyncio.CancelledError:
            pass
        finally:
            emit_runtime_event(
                self.db,
                kind="orchestrator_stopping",
                severity="info",
                payload={},
            )

    async def stop(self) -> None:
        await self.watchdog.stop()

    async def _await_all(self) -> None:
        tasks = [a.task for a in self.watchdog.agents.values() if a.task is not None]
        if not tasks:
            return
        # Esperamos con timeout de seguridad (los loops de --once deben
        # terminar rápido).
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=120.0,
            )
        except asyncio.TimeoutError:
            logger.warning("--once tardó >120s; cancelando tasks restantes")


# ---------------------------------------------------------------------------
# Helpers de loading
# ---------------------------------------------------------------------------


def load_amazon_seeds(settings: Settings) -> list[str]:
    return _load_seeds_file("config/seeds/amazon.json")


def load_mercadolibre_seeds(settings: Settings) -> list[str]:
    return _load_seeds_file(settings.mercadolibre_seeds_path)


def _load_seeds_file(path: str) -> list[str]:
    from pathlib import Path

    p = Path(path)
    if not p.is_absolute():
        from .config import PROJECT_ROOT

        p = PROJECT_ROOT / p
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("no se pudo leer seeds %s: %s", p, exc)
        return []
    if isinstance(data, list):
        return [str(x) for x in data if isinstance(x, str)]
    return []


__all__ = [
    "AgentFactoryBuilder",
    "AgentFactoryFn",
    "Orchestrator",
    "OrchestratorConfig",
    "RegistrationReport",
    "load_amazon_seeds",
    "load_mercadolibre_seeds",
]
