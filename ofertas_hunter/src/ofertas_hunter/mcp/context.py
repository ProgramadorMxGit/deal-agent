"""ServerContext del MCP server.

Encapsula los recursos compartidos entre handlers de tools:

- conexión SQLite WAL (la misma del bot)
- `Settings` resuelta
- `OperatingScheduler` (autoridad final de modo de operación)
- agentes lazy: amazon_hunter, mercadolibre_hunter, discovery_*, dispatcher,
  revalidator, publisher, browser worker singleton, evolution client
- locks por marketplace + lock global del dispatcher
- estado en memoria: pause_state, review_tokens, last_normal_publication_at

Política de instanciación: **un solo `BrowserWorker`** se comparte entre
todos los componentes que lo necesiten. Si Playwright no está disponible
los handlers reciben `BrowserUnavailableError` y reportan error estructurado.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..config import Settings
from ..runtime.scheduler import OperatingScheduler, ScheduleConfig
from .safety import PauseInfo


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Excepciones
# ---------------------------------------------------------------------------


class BrowserUnavailableError(RuntimeError):
    """Levantada cuando Playwright no se puede inicializar."""


# ---------------------------------------------------------------------------
# ServerContext
# ---------------------------------------------------------------------------


@dataclass
class ReviewSession:
    """Sesión de revisión de oferta abierta por el cliente MCP."""

    token: str
    outbox_id: int
    kind: str  # "offer_review" | "message_copy"
    snapshot_payload: dict
    created_at: datetime
    expires_at: datetime


@dataclass
class ServerContext:
    """Recursos compartidos por todos los handlers MCP."""

    db: sqlite3.Connection
    settings: Settings
    scheduler: OperatingScheduler

    # Estado en proceso
    pause_state: dict[str, PauseInfo] = field(default_factory=dict)
    review_tokens: dict[str, ReviewSession] = field(default_factory=dict)
    last_normal_publication_at: Optional[datetime] = None

    # Agentes / componentes lazy (instanciados al primer uso)
    _browser: Any = None  # legacy, fallback genérico (no Amazon ni ML)
    _amazon_browser: Any = None  # browser dedicado a Amazon (con user_data_dir)
    _ml_browser: Any = None  # browser dedicado a ML (con user_data_dir)
    _evolution_client: Any = None
    _publisher: Any = None
    _outbox_repo: Any = None
    _dispatcher: Any = None
    _amazon_hunter: Any = None
    _ml_hunter: Any = None
    _amazon_discovery: Any = None
    _ml_discovery: Any = None
    _revalidator: Any = None
    _ml_session_loaded: bool = False

    # Locks
    marketplace_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    dispatcher_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def build(cls, *, db: sqlite3.Connection, settings: Settings) -> "ServerContext":
        sched_config = ScheduleConfig.from_env(
            enabled=settings.schedule_enabled,
            timezone_name=settings.schedule_timezone,
            hibernate_start=settings.hibernate_start,
            hibernate_end=settings.hibernate_end,
            warmup_start=settings.warmup_start,
            active_start=settings.active_start,
        )
        scheduler = OperatingScheduler(sched_config)
        return cls(db=db, settings=settings, scheduler=scheduler)

    # ------------------------------------------------------------------
    # Locks por marketplace
    # ------------------------------------------------------------------

    def lock_for(self, marketplace: str) -> asyncio.Lock:
        return self.marketplace_locks.setdefault(marketplace, asyncio.Lock())

    # ------------------------------------------------------------------
    # Browser singleton
    # ------------------------------------------------------------------

    async def get_browser(self):
        """Browser genérico (sin user_data_dir). Sólo para tools que NO son
        Amazon ni ML (e.g. revalidator usado contra URLs ad-hoc).
        """
        if self._browser is not None:
            return self._browser
        try:
            from ..browser.browser_context import BrowserConfig
            from ..browser.playwright_worker import (
                PlaywrightBrowserWorker,
                PlaywrightImportError,
            )
        except Exception as exc:  # pragma: no cover
            raise BrowserUnavailableError(f"playwright_import_failed: {exc}") from exc

        try:
            worker = PlaywrightBrowserWorker(
                BrowserConfig(headless=self.settings.amazon_headless)
            )
        except PlaywrightImportError as exc:
            raise BrowserUnavailableError(str(exc)) from exc

        await worker._ensure_started()  # noqa: SLF001
        self._browser = worker
        return worker

    async def get_amazon_browser(self):
        """Browser dedicado a Amazon, con `user_data_dir` (sesión persistente
        creada manualmente por el operador con `python -m ofertas_hunter
        login --marketplace amazon`), warmup opcional y headers HTTP
        legacy. Es el mismo browser que usa `python -m ofertas_hunter run`
        en el orchestrator nativo. Sin esto, Amazon serviría captchas
        constantemente cuando el bot corre por la opción 3 del lanzador
        (orquestador_ia.py → MCP → hunt_amazon).
        """
        if self._amazon_browser is not None:
            return self._amazon_browser
        try:
            from ..browser.browser_context import BrowserConfig
            from ..browser.playwright_worker import (
                PlaywrightBrowserWorker,
                PlaywrightImportError,
            )
        except Exception as exc:  # pragma: no cover
            raise BrowserUnavailableError(f"playwright_import_failed: {exc}") from exc
        try:
            worker = PlaywrightBrowserWorker(
                BrowserConfig(
                    headless=self.settings.amazon_headless,
                    user_data_dir=self.settings.amazon_user_data_dir,
                    warmup_amazon_homepage=self.settings.amazon_warmup_homepage,
                    # Delay entre requests Amazon: replicamos el legacy
                    # AmazonScrapperIA (5–12s) leído del .env.
                    delay_between_requests_ms=(
                        self.settings.amazon_delay_between_pages_ms_min,
                        self.settings.amazon_delay_between_pages_ms_max,
                    ),
                )
            )
        except PlaywrightImportError as exc:
            raise BrowserUnavailableError(str(exc)) from exc
        await worker._ensure_started()  # noqa: SLF001
        self._amazon_browser = worker
        return worker

    async def get_ml_browser(self):
        """Browser dedicado a ML, con `user_data_dir` (sesión persistente
        que el operador creó con `python -m ofertas_hunter login
        --marketplace mercadolibre`).
        """
        if self._ml_browser is not None:
            return self._ml_browser
        try:
            from ..browser.browser_context import BrowserConfig
            from ..browser.playwright_worker import (
                PlaywrightBrowserWorker,
                PlaywrightImportError,
            )
        except Exception as exc:  # pragma: no cover
            raise BrowserUnavailableError(f"playwright_import_failed: {exc}") from exc
        try:
            worker = PlaywrightBrowserWorker(
                BrowserConfig(
                    headless=self.settings.mercadolibre_headless,
                    user_data_dir=self.settings.mercadolibre_user_data_dir,
                )
            )
        except PlaywrightImportError as exc:
            raise BrowserUnavailableError(str(exc)) from exc
        await worker._ensure_started()  # noqa: SLF001
        self._ml_browser = worker
        return worker

    # ------------------------------------------------------------------
    # Evolution + publisher + dispatcher
    # ------------------------------------------------------------------

    def get_evolution_client(self):
        if self._evolution_client is None:
            from ..publishing.evolution_client import EvolutionClient

            self._evolution_client = EvolutionClient(
                base_url=self.settings.evolution_base_url,
                api_key=self.settings.evolution_api_key,
                instance=self.settings.evolution_instance,
                dry_run=self.settings.publishing_dry_run,
            )
        return self._evolution_client

    def get_publisher(self):
        if self._publisher is None:
            from ..publishing.whatsapp_publisher import WhatsAppPublisher

            self._publisher = WhatsAppPublisher(
                client=self.get_evolution_client(),
                target_group_id=self.settings.whatsapp_group,
                enabled=self.settings.publishing_enabled,
                mercadolibre_affiliate_required=self.settings.mercadolibre_affiliate_required_for_publish,
            )
        return self._publisher

    def get_outbox_repo(self):
        if self._outbox_repo is None:
            from ..dispatching.cooldown import CooldownPolicy
            from ..dispatching.outbox import OutboxConfig, SqliteOutbox

            self._outbox_repo = SqliteOutbox(
                self.db,
                OutboxConfig(
                    revalidate_age_seconds=self.settings.whatsapp_outbox_revalidate_age_seconds,
                    cooldown=CooldownPolicy(
                        cooldown_seconds=self.settings.whatsapp_cooldown_seconds
                    ),
                ),
            )
        return self._outbox_repo

    def get_dispatcher(self):
        if self._dispatcher is not None:
            return self._dispatcher
        from ..dispatching.dispatcher import (
            OutboxDispatcher,
            make_sqlite_published_recorder,
        )

        dispatcher = OutboxDispatcher(
            outbox=self.get_outbox_repo(),
            publisher=self.get_publisher(),
            published_recorder=make_sqlite_published_recorder(self.db),
            idle_sleep_seconds=60,
            scheduler=self.scheduler,
            revalidator=None,
        )

        # Restaurar cooldown desde la última publicación normal exitosa en DB
        row = self.db.execute(
            "SELECT sent_at FROM published_messages "
            "WHERE success=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            from datetime import datetime, timezone
            try:
                sent_str = row["sent_at"] if isinstance(row, dict) else row[0]
                # Parsear ISO 8601 con Z
                sent_str = sent_str.replace("Z", "+00:00")
                last_pub = datetime.fromisoformat(sent_str)
                dispatcher.set_last_normal_publication(last_pub)
            except Exception:
                pass

        self._dispatcher = dispatcher
        return self._dispatcher

    # ------------------------------------------------------------------
    # Hunters
    # ------------------------------------------------------------------

    async def get_amazon_hunter(self):
        if self._amazon_hunter is not None:
            return self._amazon_hunter
        from ..agents.amazon_hunter_agent import AmazonHunterAgent

        browser = await self.get_amazon_browser()
        self._amazon_hunter = AmazonHunterAgent(browser=browser, db_conn=self.db)
        return self._amazon_hunter

    async def get_amazon_discovery(self):
        if self._amazon_discovery is not None:
            return self._amazon_discovery
        from ..agents.discovery_agent import DiscoveryAgent

        browser = await self.get_amazon_browser()
        self._amazon_discovery = DiscoveryAgent(
            browser=browser,
            db_conn=self.db,
            marketplace="amazon",
            max_per_cycle=4,
        )
        return self._amazon_discovery

    async def get_ml_hunter(self):
        if self._ml_hunter is not None:
            return self._ml_hunter
        from ..agents.mercadolibre_hunter_agent import MercadoLibreHunterAgent
        from ..marketplaces.mercadolibre_affiliate import (
            PlaywrightAffiliateExtractor,
        )

        browser = await self.get_ml_browser()
        await self._ensure_ml_cookies(browser)
        extractor = PlaywrightAffiliateExtractor(browser._context)  # noqa: SLF001
        self._ml_hunter = MercadoLibreHunterAgent(
            browser=browser,
            db_conn=self.db,
            affiliate_extractor=extractor,
            affiliate_required_for_publish=self.settings.mercadolibre_affiliate_required_for_publish,
        )
        return self._ml_hunter

    async def get_ml_discovery(self):
        if self._ml_discovery is not None:
            return self._ml_discovery
        from ..agents.discovery_agent import DiscoveryAgent

        browser = await self.get_ml_browser()
        await self._ensure_ml_cookies(browser)
        self._ml_discovery = DiscoveryAgent(
            browser=browser,
            db_conn=self.db,
            marketplace="mercadolibre",
            max_per_cycle=4,
        )
        return self._ml_discovery

    async def _ensure_ml_cookies(self, browser) -> None:
        if self._ml_session_loaded:
            return
        from ..session.mercadolibre_session import MercadoLibreSession

        session = MercadoLibreSession.from_settings(
            cookies_path=self.settings.mercadolibre_cookies_path,
            fallback_path=self.settings.mercadolibre_cookies_fallback_path,
        )
        cookies, health = session.load()
        if not cookies:
            logger.warning("ML cookies: no se encontraron cookies válidas")
            self._ml_session_loaded = True
            return

        # Normalizar al formato que acepta Playwright (replicando sanitize_cookie del legacy)
        def _normalize(c: dict) -> dict:
            n = {}
            # Campos requeridos
            n["name"] = c.get("name", "")
            n["value"] = c.get("value", "")
            n["domain"] = c.get("domain", "")
            n["path"] = c.get("path", "/")
            n["httpOnly"] = bool(c.get("httpOnly", False))
            n["secure"] = bool(c.get("secure", False))

            # expires: Browser extension usa 'expirationDate', Firefox usa 'expiry'
            exp = c.get("expires")
            if exp is None:
                exp = c.get("expirationDate")
            if exp is None:
                exp = c.get("expiry")
            # Si es cookie de sesión (session=True), NO incluir expires
            if c.get("session") is True:
                exp = None
            if isinstance(exp, (int, float)) and exp > 0:
                n["expires"] = float(exp)

            # sameSite: normalizar al formato que acepta Playwright
            _samesite_map = {
                "no_restriction": "None",
                "lax": "Lax",
                "strict": "Strict",
                "none": "None",
                "unspecified": "None",
            }
            ss = c.get("sameSite", "")
            if ss is None or ss == "":
                n["sameSite"] = "None"
            elif ss in ("Strict", "Lax", "None"):
                n["sameSite"] = ss
            elif isinstance(ss, str) and ss.lower() in _samesite_map:
                n["sameSite"] = _samesite_map[ss.lower()]
            else:
                n["sameSite"] = "None"

            return n

        normalized_cookies = [_normalize(c) for c in cookies if c.get("name") and "value" in c]

        # Asegurar que el browser está iniciado antes de añadir cookies
        await browser._ensure_started()  # noqa: SLF001
        ctx = browser._context  # noqa: SLF001
        if ctx is not None:
            try:
                await ctx.add_cookies(normalized_cookies)
                logger.info("ML cookies: %d cookies cargadas en el contexto", len(normalized_cookies))
            except Exception:
                logger.exception("no se pudieron cargar cookies ML")
        else:
            logger.warning("ML cookies: browser._context es None, no se pudieron cargar")
        self._ml_session_loaded = True

    async def get_revalidator(self):
        if self._revalidator is not None:
            return self._revalidator
        from ..revalidation.playwright_revalidator import PlaywrightRevalidator

        # Reusamos el browser Amazon: tiene `user_data_dir`, warmup,
        # delays largos y reusa misma pestaña. Los settings stealth son
        # inocuos para ML/otros marketplaces; los headers HTTP legacy
        # tampoco los afectan negativamente (Playwright/Chromium no
        # mandan `Sec-Fetch-Site=none` cuando hay history).
        browser = await self.get_amazon_browser()
        self._revalidator = PlaywrightRevalidator(browser=browser, db_conn=self.db)
        return self._revalidator

    # ------------------------------------------------------------------
    # Telemetría / cooldown helpers
    # ------------------------------------------------------------------

    def record_normal_publication(self, when: Optional[datetime] = None) -> None:
        self.last_normal_publication_at = when or datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        # Cancelar timers de pause
        for info in list(self.pause_state.values()):
            timer = getattr(info, "timer", None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass

        if self._evolution_client is not None:
            try:
                await self._evolution_client.aclose()
            except Exception:
                logger.exception("aclose evolution_client falló")

        if self._browser is not None:
            try:
                await self._browser.aclose()
            except Exception:
                logger.exception("aclose browser falló")

        if self._amazon_browser is not None:
            try:
                await self._amazon_browser.aclose()
            except Exception:
                logger.exception("aclose amazon_browser falló")

        if self._ml_browser is not None:
            try:
                await self._ml_browser.aclose()
            except Exception:
                logger.exception("aclose ml_browser falló")


__all__ = [
    "BrowserUnavailableError",
    "ReviewSession",
    "ServerContext",
]
