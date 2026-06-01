"""Wrapper que arranca/cierra todo el sistema de recovery ML.

Uso desde el orquestador:

    runtime = MLSessionRecoveryRuntime.from_context(ctx)
    await runtime.start()
    ...
    await runtime.monitor_tick()  # cada N segundos
    ...
    await runtime.stop()

`runtime` orquesta:

- ``MercadoLibreSessionManager``: estado + validación activa + staging +
  promote + rotate (núcleo del hot-reload).
- ``MLSessionMonitor``: detecta cookie_expiry, manda WhatsApp.
- ``MLInboundServer``: recibe ``/cookies_ml`` del admin via webhook.
- ``MLCookieReloader``: orquesta el pipeline canónico delegando en el
  manager cuando está disponible.

El callback de rotation (`rotation_callback`) llama a
``ctx.reload_ml_cookies()`` para que el browser context ML se cierre y se
recree con cookies nuevas, sin reiniciar el servicio.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from ..notifications.telegram import TelegramNotifier
from .ml_session_inbound import MLInboundServer
from .ml_session_manager import (
    MLSessionPaths,
    MLSessionStatus,
    MercadoLibreSessionManager,
    ValidationBrowserFactory,
)
from .ml_session_poller import MLEvolutionPoller, MLPollerConfig
from .ml_session_telegram_poller import MLTelegramPoller, MLTelegramPollerConfig
from .ml_session_recovery import (
    MLCookieReloader,
    MLCookieValidator,
    MLRecoveryConfig,
    MLSessionMonitor,
)


logger = logging.getLogger(__name__)


class MLSessionRecoveryRuntime:
    """Lifecycle de los componentes del recovery dentro del bot vivo."""

    def __init__(
        self,
        config: MLRecoveryConfig,
        db: Any,
        evolution_send,
        ctx: Any,
        alert_send=None,
        *,
        secrets_dir: Optional[Path] = None,
        validation_factory: Optional[ValidationBrowserFactory] = None,
        poller_config: Optional[MLPollerConfig] = None,
        telegram_poller_config: Optional[MLTelegramPollerConfig] = None,
    ) -> None:
        self.config = config
        self.db = db
        self.ctx = ctx
        self._evolution_send = evolution_send
        self._alert_send = alert_send or evolution_send

        # Resolver rutas: si el config trae `cookies_path`, lo usamos como
        # ruta activa. El staging y el profile cookies van bajo el mismo
        # directorio raíz `secrets/`.
        active_path = Path(config.cookies_path)
        if not active_path.is_absolute():
            active_path = Path.cwd() / active_path
        if secrets_dir is None:
            secrets_dir = active_path.parent
        paths = MLSessionPaths(
            staging_path=secrets_dir / "incoming_cookies" / "mercadolibre_latest.json",
            active_cookies_path=active_path,
            profile_cookies_path=secrets_dir / "browser_profiles" / "mercadolibre" / "cookies.json",
        )

        # Manager con rotation_callback hacia ctx.reload_ml_cookies.
        self.manager = MercadoLibreSessionManager(
            db=db,
            paths=paths,
            rotation_callback=self._ctx_reload_cookies,
            validation_factory=validation_factory,
        )

        self._validator = MLCookieValidator()
        self._telegram_notifier: Optional[TelegramNotifier] = None

        # El reloader delega en el manager para el pipeline seguro.
        async def _on_cookies(cookies: list[dict]) -> bool:
            reloader = MLCookieReloader(
                db=db,
                config=config,
                reload_callback=None,
                session_manager=self.manager,
            )
            return await reloader.apply(cookies)

        self._on_cookies = _on_cookies

        self.monitor = MLSessionMonitor(
            db=db, config=config, evolution_send=self._alert_send
        )
        self.server = MLInboundServer(
            config=config,
            validator=self._validator,
            on_cookies=_on_cookies,
            evolution_send=evolution_send,
            db=db,
        )

        # Poller opcional: pull contra Evolution API (útil cuando
        # Evolution corre en host remoto y no alcanza el webhook local).
        self.poller: Optional[MLEvolutionPoller] = None
        self.telegram_poller: Optional[MLTelegramPoller] = None
        if config.uses_telegram_alerts:
            self._telegram_notifier = TelegramNotifier(
                bot_token=config.telegram_bot_token,
                chat_id=config.telegram_chat_id,
                timeout_seconds=config.telegram_timeout_seconds,
            )
            telegram_poller_config = telegram_poller_config or MLTelegramPollerConfig(
                enabled=True,
                chat_id=config.telegram_chat_id or "",
                poll_interval_seconds=15,
            )
            self.telegram_poller = MLTelegramPoller(
                recovery_config=config,
                poller_config=telegram_poller_config,
                validator=self._validator,
                on_cookies=_on_cookies,
                telegram_client=self._telegram_notifier,
                db=db,
            )
        elif poller_config is not None and poller_config.enabled:
            self.poller = MLEvolutionPoller(
                recovery_config=config,
                poller_config=poller_config,
                validator=self._validator,
                on_cookies=_on_cookies,
                evolution_send=evolution_send,
                db=db,
            )

    # ------------------------------------------------------------------

    async def _ctx_reload_cookies(self, cookies: list[dict]) -> bool:
        if not hasattr(self.ctx, "reload_ml_cookies"):
            logger.warning(
                "MLSessionRecoveryRuntime: ctx no tiene reload_ml_cookies; "
                "cookies promovidas a disco pero el browser actual NO las "
                "leerá hasta el próximo arranque del context"
            )
            return True
        try:
            return bool(await self.ctx.reload_ml_cookies(cookies))
        except Exception:
            logger.exception("ctx.reload_ml_cookies falló")
            return False

    # ------------------------------------------------------------------

    async def start(self) -> None:
        # Inyectar manager en ctx para que el resto del sistema lo
        # consulte (e.g. hunter ML antes de cada ciclo).
        if self.ctx is None:
            logger.info(
                "MLSessionRecoveryRuntime: sin ctx; ml_session_manager no se inyecta"
            )
        else:
            try:
                setattr(self.ctx, "ml_session_manager", self.manager)
            except Exception:
                logger.exception("no se pudo inyectar ml_session_manager en ctx")

        if self.config.uses_telegram_alerts:
            logger.info("MLSessionRecoveryRuntime: whatsapp inbound deshabilitado por canal telegram")
        elif self.config.inbound_enabled:
            await self.server.start()
        else:
            logger.info("MLSessionRecoveryRuntime: inbound deshabilitado")

        if self.telegram_poller is not None:
            try:
                await self.telegram_poller.start()
            except Exception:
                logger.exception("MLSessionRecoveryRuntime: telegram_poller.start falló")

        if self.poller is not None:
            try:
                await self.poller.start()
            except Exception:
                logger.exception("MLSessionRecoveryRuntime: poller.start falló")

    async def stop(self) -> None:
        if self.telegram_poller is not None:
            try:
                await self.telegram_poller.stop()
            except Exception:
                logger.exception("MLSessionRecoveryRuntime.telegram_poller.stop falló")
        if self.poller is not None:
            try:
                await self.poller.stop()
            except Exception:
                logger.exception("MLSessionRecoveryRuntime.poller.stop falló")
        try:
            await self.server.stop()
        except Exception:
            logger.exception("MLSessionRecoveryRuntime.stop falló")
        if self._telegram_notifier is not None:
            try:
                await self._telegram_notifier.aclose()
            except Exception:
                logger.exception("MLSessionRecoveryRuntime.telegram_notifier.close falló")

    async def monitor_tick(self) -> Optional[str]:
        """Wrapper para que el orquestador lo llame periódicamente."""
        try:
            return await self.monitor.tick()
        except Exception:
            logger.exception("MLSessionMonitor.tick raised")
            return None

    @classmethod
    def build(
        cls,
        *,
        settings: Any,
        db: Any,
        evolution_send,
        ctx: Any,
        alert_send=None,
        validation_factory: Optional[ValidationBrowserFactory] = None,
    ) -> "MLSessionRecoveryRuntime":
        """Construye la instancia leyendo config desde Settings."""
        config = MLRecoveryConfig.from_settings(settings)
        poller_config = MLPollerConfig.from_settings(settings)
        telegram_poller_config = MLTelegramPollerConfig.from_settings(settings)
        return cls(
            config=config,
            db=db,
            evolution_send=evolution_send,
            alert_send=alert_send,
            ctx=ctx,
            validation_factory=validation_factory,
            poller_config=poller_config,
            telegram_poller_config=telegram_poller_config,
        )
