"""ServerContext del MCP server.

Encapsula los recursos compartidos entre handlers de tools:

- conexiâ”œâ”‚n SQLite WAL (la misma del bot)
- `Settings` resuelta
- `OperatingScheduler` (autoridad final de modo de operaciâ”œâ”‚n)
- agentes lazy: amazon_hunter, mercadolibre_hunter, discovery_*, dispatcher,
  revalidator, publisher, browser worker singleton, evolution client
- locks por marketplace + lock global del dispatcher
- estado en memoria: pause_state, review_tokens, last_normal_publication_at

Polâ”œÂ¡tica de instanciaciâ”œâ”‚n: **un solo `BrowserWorker`** se comparte entre
todos los componentes que lo necesiten. Si Playwright no estâ”œÃ­ disponible
los handlers reciben `BrowserUnavailableError` y reportan error estructurado.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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
    """Sesiâ”œâ”‚n de revisiâ”œâ”‚n de oferta abierta por el cliente MCP."""

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
    _browser: Any = None  # legacy, fallback genâ”œÂ®rico (no Amazon ni ML)
    _amazon_browser: Any = None  # browser dedicado a Amazon (con user_data_dir)
    _ml_browser: Any = None  # browser dedicado a ML (con user_data_dir)
    _evolution_client: Any = None
    _publisher: Any = None
    _screenshot_capturer: Any = None
    _outbox_repo: Any = None
    _dispatcher: Any = None
    _amazon_hunter: Any = None
    _ml_hunter: Any = None
    _amazon_discovery: Any = None
    _ml_discovery: Any = None
    _revalidator: Any = None
    _amazon_session_loaded: bool = False
    _ml_session_loaded: bool = False

    # Inyectado por el runtime de session recovery (opcional). Si estâ”œÃ­
    # presente, el hunter ML lo consulta antes de cada ciclo y skipea si
    # el estado no es VALID.
    ml_session_manager: Any = None

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
        """Browser genâ”œÂ®rico (sin user_data_dir). Sâ”œâ”‚lo para tools que NO son
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
        """Browser dedicado a Amazon, con `user_data_dir` (sesiâ”œâ”‚n persistente
        creada manualmente por el operador con `python -m ofertas_hunter
        login --marketplace amazon`), warmup opcional y headers HTTP
        legacy. Es el mismo browser que usa `python -m ofertas_hunter run`
        en el orchestrator nativo. Sin esto, Amazon servirâ”œÂ¡a captchas
        constantemente cuando el bot corre por la opciâ”œâ”‚n 3 del lanzador
        (orquestador_ia.py Ã”Ã¥Ã† MCP Ã”Ã¥Ã† hunt_amazon).
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
                    # AmazonScrapperIA (5Ã”Ã‡Ã´12s) leâ”œÂ¡do del .env.
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
        """Browser dedicado a ML, con `user_data_dir` (sesiâ”œâ”‚n persistente
        que el operador creâ”œâ”‚ con `python -m ofertas_hunter login
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
                api_key_header=self.settings.evolution_api_key_header,
                dry_run=self.settings.publishing_dry_run,
            )
        return self._evolution_client

    def get_screenshot_capturer(self):
        """Capturer de screenshots del PDP para la imagen de WhatsApp.

        Lazy + best-effort. Si `PUBLISH_SCREENSHOT_ENABLED=false` devuelve None
        (el publisher usará la imagen pública). Gestiona su propio navegador
        headless, separado de los browsers de hunt/revalidación.
        """
        if not getattr(self.settings, "publish_screenshot_enabled", False):
            return None
        if self._screenshot_capturer is None:
            from ..publishing.screenshot_capturer import ScreenshotCapturer

            self._screenshot_capturer = ScreenshotCapturer(
                mercadolibre_cookies_path=self.settings.mercadolibre_cookies_path,
                mercadolibre_cookies_fallback_path=getattr(
                    self.settings, "mercadolibre_cookies_fallback_path", None
                ),
                amazon_cookies_path=self.settings.amazon_cookies_path,
                headless=getattr(self.settings, "publish_screenshot_headless", True),
                nav_timeout_ms=getattr(
                    self.settings, "publish_screenshot_nav_timeout_ms", 30000
                ),
                jpeg_quality=getattr(
                    self.settings, "publish_screenshot_jpeg_quality", 85
                ),
                enabled=True,
            )
        return self._screenshot_capturer

    def get_publisher(self):
        if self._publisher is None:
            from ..publishing.whatsapp_publisher import WhatsAppPublisher

            self._publisher = WhatsAppPublisher(
                client=self.get_evolution_client(),
                target_group_id=self.settings.whatsapp_group,
                enabled=self.settings.publishing_enabled,
                mercadolibre_affiliate_required=self.settings.mercadolibre_affiliate_required_for_publish,
                amazon_affiliate_required=self.settings.amazon_affiliate_required_for_publish,
                amazon_min_discount_percent=self.settings.amazon_min_discount_percent,
                amazon_extreme_discount_threshold=self.settings.amazon_extreme_discount_threshold,
                ml_extreme_discount_threshold=getattr(self.settings, "ml_extreme_discount_threshold", 70.0),
                amazon_min_absolute_price=self.settings.amazon_min_absolute_price,
                screenshot_capturer=self.get_screenshot_capturer(),
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
        from ..dispatching.curator_factory import build_diversity_curator
        from ..dispatching.dispatcher import (
            OutboxDispatcher,
            make_sqlite_duplicate_checker,
            make_sqlite_published_recorder,
            make_stale_price_checker,
        )
        from ..revalidation.playwright_revalidator import PlaywrightRevalidator

        dup_checker = make_sqlite_duplicate_checker(self.db, hours=48)
        stale_checker = make_stale_price_checker(max_age_hours=4)

        def _combined_checker(item):
            return dup_checker(item) or stale_checker(item)

        # Revalidador lazy: se construye con el browser Amazon en el primer uso.
        # Capturamos `self` (el ServerContext) en el closure.
        _ctx_ref = self
        _rev_instance: list = []

        class _LazyRevalidator:
            async def revalidate(self, item):
                if not _rev_instance:
                    try:
                        browser = await _ctx_ref.get_amazon_browser()
                        _rev_instance.append(
                            PlaywrightRevalidator(
                                browser=browser,
                                db_conn=_ctx_ref.db,
                            )
                        )
                    except Exception as exc:
                        logger.warning(
                            "LazyRevalidator: no se pudo inicializar browser (%s) "
                            "Ã”Ã‡Ã¶ item %s se mantiene pending",
                            exc,
                            item.id,
                        )
                        # Retornar still_eligible=True para no descartar el item
                        from ..dispatching.dispatcher import RevalidationResult
                        return RevalidationResult(still_eligible=True, payload=item.message_payload)
                return await _rev_instance[0].revalidate(item)

        # Diversity curator: si estâ”œÃ­ habilitado en settings, se inyecta como
        # `item_selector`; cuando es None el dispatcher mantiene su path
        # legacy (`pick_random_eligible`).
        curator = build_diversity_curator(self.db, self.settings)
        item_selector = curator.pick if curator is not None else None

        dispatcher = OutboxDispatcher(
            outbox=self.get_outbox_repo(),
            publisher=self.get_publisher(),
            published_recorder=make_sqlite_published_recorder(self.db),
            duplicate_checker=_combined_checker,
            item_selector=item_selector,
            idle_sleep_seconds=60,
            scheduler=self.scheduler,
            revalidator=_LazyRevalidator(),
        )

        # Restaurar cooldown desde la â”œâ•‘ltima publicaciâ”œâ”‚n normal exitosa en DB
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

        # Switch criterio B: si `amazon_hunter_legacy=True`, instanciamos
        # `LegacyAmazonHunterAgent` (browser efâ”œÂ¡mero, receta anti-captcha
        # del scraper original). Si estâ”œÃ­ en False (default), usamos el
        # `AmazonHunterAgent` con browser persistente.
        #
        # IMPORTANTE: este es el verdadero punto de inyecciâ”œâ”‚n que usa
        # tanto la opciâ”œâ”‚n [3] (orquestador_ia.py Ã”Ã¥Ã† MCP) como las
        # opciones [1] y [2] (kiro-cli Ã”Ã¥Ã† MCP). El switch en
        # `orchestrator.py` solo aplica al modo `python -m ofertas_hunter
        # run` directo.
        legacy_flag = bool(getattr(self.settings, "amazon_hunter_legacy", False))
        if legacy_flag:
            from ..agents.legacy_amazon_hunter_agent import LegacyAmazonHunterAgent

            self._amazon_hunter = LegacyAmazonHunterAgent(
                db_conn=self.db,
                warmup_homepage=True,
                delay_between_requests_ms=(
                    getattr(self.settings, "amazon_delay_between_pages_ms_min", 8000),
                    getattr(self.settings, "amazon_delay_between_pages_ms_max", 15000),
                ),
            )
            return self._amazon_hunter

        from ..agents.amazon_hunter_agent import AmazonHunterAgent
        from ..marketplaces.amazon_affiliate import PlaywrightAffiliateExtractor

        browser = await self.get_amazon_browser()
        await self._ensure_amazon_cookies(browser)
        extractor = PlaywrightAffiliateExtractor(browser._context)  # noqa: SLF001
        self._amazon_hunter = AmazonHunterAgent(
            browser=browser,
            db_conn=self.db,
            affiliate_extractor=extractor,
        )
        return self._amazon_hunter

    async def get_amazon_affiliate_enricher(self):
        """Enricher de afiliados Amazon (SiteStripe) sobre el outbox.

        Reutiliza el browser persistente de Amazon (con cookies de afiliado)
        y el `PlaywrightAffiliateExtractor`. Se instancia en cada llamada
        porque es liviano y stateless salvo el extractor.
        """
        from ..agents.amazon_affiliate_enricher import AmazonAffiliateEnricher
        from ..marketplaces.amazon_affiliate import PlaywrightAffiliateExtractor

        browser = await self.get_amazon_browser()
        await self._ensure_amazon_cookies(browser)
        extractor = PlaywrightAffiliateExtractor(browser._context)  # noqa: SLF001
        return AmazonAffiliateEnricher(self.db, extractor)

    @property
    def amazon_profile_lock_path(self) -> str:
        """Ruta del filelock que serializa el uso del perfil Chromium de Amazon.

        Cubre cualquier proceso (orquestador + CLI) que abra
        `launch_persistent_context` sobre `secrets/browser_profiles/amazon`.
        """
        if getattr(self, "_amazon_profile_lock_path", None):
            return self._amazon_profile_lock_path
        base = self.settings.amazon_user_data_dir or "secrets/browser_profiles/amazon"
        return str(Path(base) / ".profile.lock")

    @amazon_profile_lock_path.setter
    def amazon_profile_lock_path(self, value: str) -> None:
        self._amazon_profile_lock_path = value

    async def get_amazon_discovery(self):
        if self._amazon_discovery is not None:
            return self._amazon_discovery
        from ..agents.discovery_agent import DiscoveryAgent

        # Mismo switch que get_amazon_hunter: si flag legacy estâ”œÃ­ ON,
        # el discovery tambiâ”œÂ®n usa el browser efâ”œÂ¡mero anti-captcha
        # (LegacyDiscoveryBrowser) en lugar del browser persistente
        # marcado por Amazon. Esto resuelve los CAPTCHAs en URLs
        # `/s?k=...` (bâ”œâ•‘squedas Amazon) que el DiscoveryAgent
        # procesa.
        legacy_flag = bool(getattr(self.settings, "amazon_hunter_legacy", False))
        if legacy_flag:
            from ..agents.legacy_amazon.discovery_browser_adapter import (
                LegacyDiscoveryBrowser,
            )

            browser = LegacyDiscoveryBrowser(
                headless=getattr(self.settings, "amazon_headless", True),
                warmup_homepage=True,
                delay_between_requests_ms=(
                    getattr(self.settings, "amazon_delay_between_pages_ms_min", 8000),
                    getattr(self.settings, "amazon_delay_between_pages_ms_max", 15000),
                ),
            )
            self._amazon_discovery = DiscoveryAgent(
                browser=browser,
                db_conn=self.db,
                marketplace="amazon",
                max_per_cycle=4,
            )
            return self._amazon_discovery

        browser = await self.get_amazon_browser()
        await self._ensure_amazon_cookies(browser)
        self._amazon_discovery = DiscoveryAgent(
            browser=browser,
            db_conn=self.db,
            marketplace="amazon",
            max_per_cycle=4,
        )
        return self._amazon_discovery

    async def _ensure_amazon_cookies(self, browser) -> None:
        if self._amazon_session_loaded:
            return
        from ..session.amazon_session import AmazonSession

        session = AmazonSession.from_settings(
            cookies_path=self.settings.amazon_cookies_path,
        )
        health = await session.inject_into_browser(browser)
        if health.is_missing or health.is_empty:
            logger.warning("Amazon cookies: no se encontraron cookies validas")
        self._amazon_session_loaded = True

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
            session_manager=getattr(self, "ml_session_manager", None),
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
            logger.warning("ML cookies: no se encontraron cookies vâ”œÃ­lidas")
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
            # Si es cookie de sesiâ”œâ”‚n (session=True), NO incluir expires
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

        # Asegurar que el browser estâ”œÃ­ iniciado antes de aâ”œâ–’adir cookies
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

    async def reload_ml_cookies(self, cookies: Optional[list[dict]] = None) -> bool:
        """Hot-reload de cookies ML sin reiniciar el bot.

        - Si `cookies` viene como argumento, las inyecta directamente en
          el browser ML actual (despuâ”œÂ®s de limpiar las viejas).
        - Si no, lee `secrets/mercadolibre_cookies.json` desde disco.
        - Resetea `_ml_session_loaded=False` para que `_ensure_ml_cookies`
          recargue al prâ”œâ”‚ximo `get_ml_hunter()` / `get_ml_discovery()`.
        - Resetea el flag `paused` del `MercadoLibreHunterAgent` si existe.
        - Emite `runtime_event(kind="ml_cookies_reloaded")`.

        Retorna True si la inyecciâ”œâ”‚n + reset funcionaron.
        """
        from ..session.mercadolibre_session import MercadoLibreSession

        # 1) Resolver lista de cookies a inyectar
        if cookies is None:
            session = MercadoLibreSession.from_settings(
                cookies_path=self.settings.mercadolibre_cookies_path,
                fallback_path=self.settings.mercadolibre_cookies_fallback_path,
            )
            cookies, _health = session.load()
            if not cookies:
                logger.warning("reload_ml_cookies: no hay cookies en disco")
                return False

        # 2) Si no hay browser ML aâ”œâ•‘n, sâ”œâ”‚lo invalidamos el flag para que la
        #    prâ”œâ”‚xima inicializaciâ”œâ”‚n lea las cookies frescas.
        browser = self._ml_browser
        if browser is None:
            self._ml_session_loaded = False
            logger.info(
                "reload_ml_cookies: browser ML aâ”œâ•‘n no iniciado; cookies se "
                "cargarâ”œÃ­n al primer get_ml_*",
            )
            return True

        ctx = getattr(browser, "_context", None)
        if ctx is None:
            self._ml_session_loaded = False
            logger.warning(
                "reload_ml_cookies: browser ML sin context; flag invalidado"
            )
            return True

        # 3) Limpiar cookies viejas del context (rotaciâ”œâ”‚n in-place).
        try:
            await ctx.clear_cookies()
        except Exception:
            logger.exception("reload_ml_cookies: clear_cookies fallâ”œâ”‚")

        # 4) Inyectar las nuevas usando el normalizador estâ”œÃ­ndar.
        self._ml_session_loaded = False
        try:
            await self._ensure_ml_cookies(browser)
        except Exception:
            logger.exception("reload_ml_cookies: _ensure_ml_cookies fallâ”œâ”‚")
            return False

        # 5) Resetear flag `paused` del hunter ML si estâ”œÃ­ cacheado.
        hunter = self._ml_hunter
        if hunter is not None and hasattr(hunter, "_paused"):
            try:
                hunter._paused = False  # noqa: SLF001
            except Exception:
                pass

        # 6) Emit evento explâ”œÂ¡cito (defensivo: el manager tambiâ”œÂ®n emite
        #    `ml_cookies_promoted` y `ml_session_context_rotated`, pero
        #    queremos un breadcrumb claro de que el ctx ya tiene cookies
        #    nuevas).
        try:
            from datetime import datetime as _dt, timezone as _tz
            import json as _json

            now_iso = (
                _dt.now(_tz.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            self.db.execute(
                "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    "ml_cookies_reloaded",
                    "info",
                    _json.dumps(
                        {"cookies_count": len(cookies)},
                        ensure_ascii=False,
                    ),
                    now_iso,
                ),
            )
            self.db.commit()
        except Exception:
            logger.exception("reload_ml_cookies: emit event fallâ”œâ”‚")

        return True

    async def get_revalidator(self):
        if self._revalidator is not None:
            return self._revalidator
        from ..revalidation.playwright_revalidator import PlaywrightRevalidator

        # Reusamos el browser Amazon: tiene `user_data_dir`, warmup,
        # delays largos y reusa misma pestaâ”œâ–’a. Los settings stealth son
        # inocuos para ML/otros marketplaces; los headers HTTP legacy
        # tampoco los afectan negativamente (Playwright/Chromium no
        # mandan `Sec-Fetch-Site=none` cuando hay history).
        browser = await self.get_amazon_browser()
        self._revalidator = PlaywrightRevalidator(browser=browser, db_conn=self.db)
        return self._revalidator

    # ------------------------------------------------------------------
    # Telemetrâ”œÂ¡a / cooldown helpers
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
                logger.exception("aclose evolution_client fallâ”œâ”‚")

        # Si el hunter Amazon en uso es el legacy, lo cerramos
        # explâ”œÂ¡citamente: tiene su propio worker efâ”œÂ¡mero que NO estâ”œÃ­
        # registrado en `_amazon_browser`.
        if self._amazon_hunter is not None:
            try:
                close = getattr(self._amazon_hunter, "aclose", None)
                if close is not None:
                    await close()
            except Exception:
                logger.exception("aclose amazon_hunter fallâ”œâ”‚")

        # Anâ”œÃ­logo para el discovery legacy: el LegacyDiscoveryBrowser
        # tiene su propio LegacyAmazonWorker interno que hay que
        # cerrar.
        if self._amazon_discovery is not None:
            try:
                close = getattr(self._amazon_discovery, "aclose", None)
                if close is not None:
                    await close()
            except Exception:
                logger.exception("aclose amazon_discovery fallâ”œâ”‚")

        if self._browser is not None:
            try:
                await self._browser.aclose()
            except Exception:
                logger.exception("aclose browser fallâ”œâ”‚")

        if self._amazon_browser is not None:
            try:
                await self._amazon_browser.aclose()
            except Exception:
                logger.exception("aclose amazon_browser fallâ”œâ”‚")

        if self._ml_browser is not None:
            try:
                await self._ml_browser.aclose()
            except Exception:
                logger.exception("aclose ml_browser fallâ”œâ”‚")

        if self._screenshot_capturer is not None:
            try:
                await self._screenshot_capturer.aclose()
            except Exception:
                logger.exception("aclose screenshot_capturer falló")


__all__ = [
    "BrowserUnavailableError",
    "ReviewSession",
    "ServerContext",
]

