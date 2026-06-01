"""Implementación real del BrowserWorker con Playwright Chromium headless.

Importa Playwright **lazy** (sólo al instanciar). Esto permite que los tests
y el resto del paquete arranquen sin Playwright instalado.

Defaults sensatos:

- Chromium headless.
- UA + viewport aleatorios (rotación).
- `STEALTH_SCRIPT` aplicado vía `add_init_script`.
- Bloqueo de `media` y `font` para acelerar la carga sin romper imágenes
  (las `image` se mantienen porque el revalidator necesita confirmar la
  imagen del producto).
- Jitter entre requests `(1.5s, 4.5s)`.
- Backoff exponencial cuando detecta página de captcha.

El worker NO hace scraping masivo por sí solo: es un fetcher por URL. La
política de exploración (frontier, seeds, retries) vive en el agente hunter.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

from .browser_context import (
    STEALTH_SCRIPT,
    BrowserConfig,
    RenderedPage,
)
from .amazon_captcha_detector import AmazonCaptchaDetector


logger = logging.getLogger(__name__)


# Tokens genéricos (ML, fallback). Para Amazon usamos AmazonCaptchaDetector
# que es estructural — ver `amazon_captcha_detector.py`. Mantenemos esta
# lista corta para Mercado Libre (donde el matching simple ya funcionaba).
_ML_CAPTCHA_TOKENS = (
    "/auth/security/captcha",
    "amzn-captcha",  # ML a veces sirve fallback de Amazon en widgets
)


_AMAZON_DOMAINS = ("amazon.com.mx", "amazon.com", "www.amazon.com")


class PlaywrightImportError(RuntimeError):
    pass


def _require_playwright():
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception as exc:  # pragma: no cover - sólo runtime real
        raise PlaywrightImportError(
            "Playwright no está instalado. Ejecuta `pip install playwright "
            "&& playwright install chromium`."
        ) from exc
    return async_playwright


class PlaywrightBrowserWorker:
    """BrowserWorker real usando Playwright Chromium."""

    def __init__(self, config: Optional[BrowserConfig] = None) -> None:
        self.config = config or BrowserConfig()
        self._playwright = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()
        self._last_fetch_at: float = 0.0
        # AmazonCaptchaDetector se aplica SOLO a URLs Amazon. ML conserva
        # su detector original (tokens) que ya estaba afinado para sesión.
        self._amazon_captcha_detector = AmazonCaptchaDetector()
        # Page persistente reutilizada entre fetches (legacy
        # `AmazonScrapperIA/src/browser_worker.py`: el legacy navega URL
        # tras URL en la MISMA pestaña en lugar de abrir y cerrar una
        # nueva por fetch). Abrir/cerrar pestañas constantemente es una
        # señal fuerte de automation que Amazon usa para servir captchas.
        self._page = None
        # Backoff post-captcha (legacy `_handle_captcha`). Si Amazon nos
        # da captcha, la siguiente URL debe esperar más tiempo para que
        # Amazon "olvide" el rate limit. Crece exponencialmente.
        self._consecutive_amazon_captchas = 0
        self._captcha_backoff_until: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "PlaywrightBrowserWorker":
        await self._ensure_started()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    async def _ensure_started(self) -> None:
        if self._browser is not None or self._context is not None:
            return
        async_playwright = _require_playwright()
        self._playwright = await async_playwright().start()
        ua = self.config.random_user_agent()
        viewport = self.config.random_viewport()
        # Args heredados del legacy AmazonScrapperIA. La key
        # `--disable-blink-features=AutomationControlled` es la que más
        # impacto tiene contra detection vectores tipo `navigator.webdriver`.
        launch_args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--no-first-run",
            "--disable-default-apps",
            "--disable-infobars",
            "--window-size=1920,1080",
            "--start-maximized",
            f"--user-agent={ua}",
        ]

        if self.config.user_data_dir:
            # Sesión persistente: el contexto vive en disco entre runs.
            # No hay browser separado; `launch_persistent_context` retorna
            # directamente un `BrowserContext`.
            from pathlib import Path as _Path

            user_data_dir = _Path(self.config.user_data_dir).expanduser()
            user_data_dir.mkdir(parents=True, exist_ok=True)
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=self.config.headless,
                args=launch_args,
                # Quita el flag --enable-automation que Playwright añade por
                # defecto. Eso elimina la barra amarilla "Un software de
                # prueba automatizado está controlando Chrome" y reduce la
                # probabilidad de que ML/Amazon detecten automation.
                ignore_default_args=["--enable-automation"],
                user_agent=ua,
                viewport=viewport,
                locale=self.config.locale,
                timezone_id=self.config.timezone,
                extra_http_headers=dict(self.config.extra_http_headers_amazon),
            )
            # Con persistent context no hay `browser` separado; el caller
            # cierra `context` y eso libera todo.
            self._browser = None
            logger.info(
                "Browser persistente iniciado: profile=%s headless=%s",
                user_data_dir,
                self.config.headless,
            )
        else:
            self._browser = await self._playwright.chromium.launch(
                headless=self.config.headless,
                args=launch_args,
                ignore_default_args=["--enable-automation"],
            )
            # Headers extra: legacy los inyecta directo en el contexto. Aquí
            # los aplicamos siempre porque Playwright/Chromium MX webrequest
            # los acepta sin disparar nada en ML.
            self._context = await self._browser.new_context(
                user_agent=ua,
                viewport=viewport,
                locale=self.config.locale,
                timezone_id=self.config.timezone,
                extra_http_headers=dict(self.config.extra_http_headers_amazon),
            )
        await self._context.add_init_script(STEALTH_SCRIPT)
        # Bloqueo de recursos
        if self.config.block_resource_types:
            await self._context.route(
                "**/*",
                lambda route, request: (
                    route.abort()
                    if request.resource_type in self.config.block_resource_types
                    else route.continue_()
                ),
            )
        # Warmup opcional (legacy AmazonScrapperIA): visita homepage para
        # establecer cookies anónimas. Sólo si está habilitado para no
        # romper ML / tests de CI.
        if self.config.warmup_amazon_homepage:
            try:
                await self._warmup_amazon()
            except Exception as exc:
                logger.warning("Amazon warmup falló: %s", exc)

    async def _warmup_amazon(self) -> None:
        """Carga `https://www.amazon.com.mx` antes del primer fetch real.

        Replica `BrowserWorker._warmup` del legacy: la idea es darle al
        contexto cookies y aspect de sesión legítima.
        """
        assert self._context is not None
        page = await self._context.new_page()
        try:
            await page.goto(
                "https://www.amazon.com.mx",
                wait_until="domcontentloaded",
                timeout=self.config.page_load_timeout_ms,
            )
            # Pequeña pausa humana para asentar cookies.
            await asyncio.sleep(random.uniform(2.0, 4.0))
            try:
                await page.evaluate("window.scrollTo(0, 800)")
                await asyncio.sleep(random.uniform(0.5, 1.0))
            except Exception:
                pass
            logger.info("Amazon warmup OK (homepage cookies set)")
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def aclose(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:
            pass
        self._context = None
        self._browser = None
        self._playwright = None
        self._page = None

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    async def fetch(self, url: str) -> RenderedPage:
        async with self._lock:
            await self._ensure_started()
            # Si venimos de captcha, espera el backoff antes de la próxima
            # URL. Esto replica el `_handle_captcha` del legacy.
            await self._respect_captcha_backoff()
            await self._respect_rate_limit()
            return await self._do_fetch(url)

    async def _respect_captcha_backoff(self) -> None:
        if self._captcha_backoff_until <= 0:
            return
        now = time.monotonic()
        if now < self._captcha_backoff_until:
            wait = self._captcha_backoff_until - now
            logger.warning(
                "Backoff post-captcha activo: esperando %.1fs antes del próximo fetch",
                wait,
            )
            await asyncio.sleep(wait)
        self._captcha_backoff_until = 0.0

    async def _respect_rate_limit(self) -> None:
        delta_low, delta_high = self.config.delay_between_requests_ms
        target_delta = random.uniform(delta_low, delta_high) / 1000.0
        elapsed = time.monotonic() - self._last_fetch_at
        if elapsed < target_delta:
            await asyncio.sleep(target_delta - elapsed)
        self._last_fetch_at = time.monotonic()

    async def _do_fetch(self, url: str) -> RenderedPage:
        assert self._context is not None
        # Reusamos la misma page entre fetches (legacy AmazonScrapperIA).
        page = await self._get_or_create_page()
        started = time.monotonic()
        screenshot_bytes: Optional[bytes] = None
        try:
            response = await page.goto(
                url,
                timeout=self.config.page_load_timeout_ms,
                wait_until="domcontentloaded",
            )
            # Pausa post-goto (legacy `_navigate`): simula que un humano
            # mira la página antes de interactuar. Reduce señales de
            # bot fetch-and-leave.
            if response is not None and response.status == 200:
                await asyncio.sleep(random.uniform(0.8, 1.5))
            elif response is not None and response.status in (301, 302):
                await asyncio.sleep(random.uniform(0.5, 1.0))
            html = await page.content()
            status = response.status if response is not None else 0
            final_url = page.url

            # Comportamiento humano (legacy AmazonScrapperIA): scroll
            # incremental + pausas + mouse jitter para que Amazon no
            # detecte un bot fetch-and-leave. Esto es lo que el legacy
            # hacía y lo que evitaba captchas en background.
            if self._is_amazon_url(url) and status == 200:
                try:
                    await self._simulate_human_browsing(page)
                    # Refresh HTML después del scroll porque algunas
                    # secciones cargan lazy (precio, imágenes alternas,
                    # reviews) sólo cuando el viewport las atraviesa.
                    html = await page.content()
                except Exception as exc:
                    logger.debug("simulate_human_browsing falló: %s", exc)

            # Reintento Amazon 503 (legacy): espera 15–30s y reintenta UNA vez.
            if (
                status == 503
                and self.config.amazon_retry_on_503
                and self._is_amazon_url(url)
            ):
                wait_low, wait_high = self.config.amazon_retry_wait_range_seconds
                wait_secs = random.uniform(wait_low, wait_high)
                logger.warning(
                    "Amazon 503 en %s — reintentando en %.1fs (legacy retry)",
                    url,
                    wait_secs,
                )
                await asyncio.sleep(wait_secs)
                try:
                    response2 = await page.goto(
                        url,
                        timeout=self.config.page_load_timeout_ms,
                        wait_until="domcontentloaded",
                    )
                    html = await page.content()
                    status = response2.status if response2 is not None else status
                    final_url = page.url
                except Exception as retry_exc:
                    logger.debug("Amazon 503 retry falló: %s", retry_exc)

            blocked, error, captcha_extras = self._evaluate_block(
                requested_url=url,
                final_url=final_url,
                html=html,
                status=status,
            )
            if blocked:
                recovery = await self._attempt_amazon_captcha_recovery(
                    url=url,
                    current_page=page,
                )
                if recovery is not None:
                    if not recovery.blocked:
                        logger.info(
                            "Amazon captcha recovery succeeded for %s after fresh-page retry",
                            url,
                        )
                        return recovery
                    html = recovery.html
                    status = recovery.status
                    final_url = recovery.final_url
                    error = recovery.error
                    screenshot_bytes = recovery.screenshot_bytes
                    captcha_extras = recovery.extras
                    page = await self._get_or_create_page()
                # Backoff exponencial post-captcha (legacy _handle_captcha):
                # 30s → 60s → 120s → 300s (cap). Reset cuando hay éxito.
                self._consecutive_amazon_captchas += 1
                wait_seconds = min(
                    30 * (2 ** (self._consecutive_amazon_captchas - 1)),
                    300,
                )
                self._captcha_backoff_until = time.monotonic() + wait_seconds
                logger.warning(
                    "Captcha confirmado en %s "
                    "(strong=%s visible=%s confidence=%s should_pause=%s) "
                    "— próximo fetch espera %ds",
                    url,
                    captcha_extras.get("strong_signals"),
                    captcha_extras.get("visible_signals"),
                    captcha_extras.get("confidence"),
                    captcha_extras.get("should_pause_marketplace"),
                    wait_seconds,
                )
                if self.config.capture_screenshot_on_failure:
                    try:
                        screenshot_bytes = await page.screenshot(full_page=False)
                    except Exception:
                        pass
            else:
                # Reset el contador si tuvimos éxito (status 200 sin captcha).
                if status == 200 and self._is_amazon_url(url):
                    self._consecutive_amazon_captchas = 0
                if captcha_extras.get("is_captcha"):
                    # Sospecha sin confianza alta. NO bloqueamos. El caller
                    # (agent) decide si guardar snapshot y emitir evento.
                    logger.info(
                        "Captcha sospechoso (no confirmado) en %s confidence=%s",
                        url,
                        captcha_extras.get("confidence"),
                    )

            duration_ms = int((time.monotonic() - started) * 1000)
            return RenderedPage(
                url=url,
                final_url=final_url,
                status=status,
                html=html,
                screenshot_bytes=screenshot_bytes,
                error=error,
                blocked=blocked,
                duration_ms=duration_ms,
                extras=captcha_extras,
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            if self.config.capture_screenshot_on_failure:
                try:
                    screenshot_bytes = await page.screenshot(full_page=False)
                except Exception:
                    pass
            # Si la page murió por un error, descartarla para que el
            # próximo fetch cree una nueva. No la cerramos aquí
            # porque puede ya estar cerrada y daría otro error.
            try:
                if page.is_closed():
                    self._page = None
            except Exception:
                self._page = None
            return RenderedPage(
                url=url,
                final_url=url,
                status=0,
                html="",
                screenshot_bytes=screenshot_bytes,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=duration_ms,
            )
        # IMPORTANTE: ya no cerramos la page aquí. El legacy reusa la
        # misma pestaña para todas las navegaciones. Cerrar/abrir
        # constantemente es señal fuerte de automation.

    async def _attempt_amazon_captcha_recovery(
        self,
        *,
        url: str,
        current_page,
    ) -> Optional[RenderedPage]:
        """Reintenta una vez con pestaña nueva antes de declarar captcha final.

        Mitiga challenges transitorios donde Amazon responde un interstitial
        real al primer hit pero permite la PDP al refrescar en un contexto ya
        autenticado. No relaja el detector: sólo evita que un captcha puntual
        derribe el ciclo cuando el segundo intento ya carga la página real.
        """
        if not self._is_amazon_url(url):
            return None
        assert self._context is not None

        try:
            try:
                if self._page is current_page:
                    self._page = None
                await current_page.close()
            except Exception:
                pass

            wait_seconds = self._captcha_retry_wait_seconds()
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)

            retry_page = await self._context.new_page()
            self._page = retry_page
            response = await retry_page.goto(
                url,
                timeout=self.config.page_load_timeout_ms,
                wait_until="domcontentloaded",
            )
            await retry_page.wait_for_timeout(3000)
            html = await retry_page.content()
            status = response.status if response is not None else 0
            final_url = retry_page.url
            blocked, error, captcha_extras = self._evaluate_block(
                requested_url=url,
                final_url=final_url,
                html=html,
                status=status,
            )

            screenshot_bytes: Optional[bytes] = None
            if blocked and self.config.capture_screenshot_on_failure:
                try:
                    screenshot_bytes = await retry_page.screenshot(full_page=False)
                except Exception:
                    pass

            return RenderedPage(
                url=url,
                final_url=final_url,
                status=status,
                html=html,
                screenshot_bytes=screenshot_bytes,
                error=error,
                blocked=blocked,
                extras=captcha_extras,
            )
        except Exception as exc:
            logger.debug("Amazon captcha recovery retry failed for %s: %s", url, exc)
            return None

    def _captcha_retry_wait_seconds(self) -> float:
        return random.uniform(2.0, 4.0)

    async def _get_or_create_page(self):
        """Devuelve la page persistente, creándola si no existe o si
        Playwright reporta que está cerrada."""
        assert self._context is not None
        page = self._page
        if page is not None:
            try:
                if page.is_closed():
                    page = None
            except Exception:
                page = None
        if page is None:
            page = await self._context.new_page()
            self._page = page
        return page

    # ------------------------------------------------------------------
    # Detección de bloqueo (captcha / sesión expirada)
    # ------------------------------------------------------------------

    def _evaluate_block(
        self,
        *,
        requested_url: str,
        final_url: str,
        html: str,
        status: int,
    ) -> tuple[bool, Optional[str], dict]:
        """Devuelve (blocked, error, extras).

        - Para URLs Amazon usa `AmazonCaptchaDetector`.
        - Para el resto, mantiene el matching simple por tokens (ML).
        """
        if self._is_amazon_url(requested_url) or self._is_amazon_url(final_url):
            assessment = self._amazon_captcha_detector.assess(
                html=html, final_url=final_url, status=status
            )
            extras = {
                "captcha_assessment": {
                    "is_captcha": assessment.is_captcha,
                    "confidence": assessment.confidence,
                    "strong_signals": list(assessment.strong_signals),
                    "weak_signals": list(assessment.weak_signals),
                    "visible_signals": list(assessment.visible_signals),
                    "should_pause_marketplace": assessment.should_pause_marketplace,
                    "reasons": list(assessment.reasons),
                },
                "is_captcha": assessment.is_captcha,
                "confidence": assessment.confidence,
                "strong_signals": list(assessment.strong_signals),
                "weak_signals": list(assessment.weak_signals),
                "visible_signals": list(assessment.visible_signals),
                "should_pause_marketplace": assessment.should_pause_marketplace,
            }
            if assessment.is_high_confidence:
                return True, "captcha_detected", extras
            if assessment.is_captcha:
                # Confianza media o baja: NO marcamos como blocked. El
                # caller verá `extras["captcha_assessment"]` y decidirá.
                return False, None, extras
            return False, None, extras

        # Resto (ML, otros): matching legacy con tokens cortos.
        if html and any(tok in html for tok in _ML_CAPTCHA_TOKENS):
            return True, "captcha_detected", {"is_captcha": True, "confidence": "high"}
        return False, None, {}

    @staticmethod
    def _is_amazon_url(url: Optional[str]) -> bool:
        if not url:
            return False
        u = url.lower()
        return any(d in u for d in _AMAZON_DOMAINS)

    async def _simulate_human_browsing(self, page) -> None:
        """Simula comportamiento humano básico para reducir señales de
        automation antes de que Amazon evalúe la sesión.

        Replica `_human_scroll` y `_move_mouse_randomly` del legacy
        `AmazonScrapperIA/src/browser_worker.py`. Ese era el ingrediente
        que hacía que el legacy no recibiera captchas en background.
        """
        try:
            height = await page.evaluate("document.body.scrollHeight") or 0
        except Exception:
            height = 0
        # Scroll incremental: pasos 200-500px con micro pausas. Cap en 3000
        # para no quedarnos eternos en páginas largas.
        target = min(int(height) if height else 1500, 3000)
        current = 0
        while current < target:
            step = random.randint(200, 500)
            current += step
            try:
                await page.evaluate(f"window.scrollTo(0, {current})")
            except Exception:
                break
            await asyncio.sleep(random.uniform(0.1, 0.4))
        # Scroll parcial hacia arriba (gesto humano típico).
        if random.random() > 0.5:
            try:
                await page.evaluate(
                    f"window.scrollTo(0, {random.randint(100, 400)})"
                )
            except Exception:
                pass
            await asyncio.sleep(random.uniform(0.2, 0.5))
        # Mouse jitter en 2-4 posiciones aleatorias.
        try:
            vp = page.viewport_size or {"width": 1366, "height": 768}
            for _ in range(random.randint(2, 4)):
                x = random.randint(100, max(101, vp["width"] - 100))
                y = random.randint(100, max(101, vp["height"] - 100))
                await page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.05, 0.2))
        except Exception:
            pass
