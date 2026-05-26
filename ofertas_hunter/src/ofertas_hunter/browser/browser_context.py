"""Interfaces y configuración del browser worker.

`BrowserWorker` es un Protocol mínimo: dado una URL, devolver `RenderedPage`
con `html`, `final_url`, `status`, `screenshot_bytes` opcional. Esto permite
que tests usen `FakeBrowserWorker` sin Playwright.

`PlaywrightBrowserWorker` (en `playwright_worker.py`) es la implementación
real con stealth, rate limit, jitter, backoff.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


# User agents Chrome reales (heredados de AmazonScrapperIA).
USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.207 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.122 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.128 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.207 Safari/537.36",
)

VIEWPORTS: tuple[dict, ...] = (
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
)


# Stealth script (heredado de AmazonScrapperIA, ligeramente simplificado).
STEALTH_SCRIPT = """
() => {
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'languages', {get: () => ['es-MX', 'es', 'en-US', 'en']});
    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
    Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
    Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
    Object.defineProperty(navigator, 'vendor', {get: () => 'Google Inc.'});
    window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
}
"""


@dataclass
class BrowserConfig:
    headless: bool = True
    locale: str = "es-MX"
    timezone: str = "America/Mexico_City"
    viewport: Optional[dict] = None  # None = aleatorio
    user_agent: Optional[str] = None  # None = aleatorio
    page_load_timeout_ms: int = 30000
    # Delays entre requests (legacy AmazonScrapperIA: 5–12s entre páginas
    # con distribución gauss). Mantenemos default conservador para no
    # romper tests que dependen del rate actual; el orchestrator lo
    # ajusta arriba.
    delay_between_requests_ms: tuple[int, int] = (1500, 4500)
    backoff_seconds_on_block: tuple[int, ...] = (30, 60, 120, 300)
    # Por defecto NO bloqueamos recursos. El legacy AmazonScrapperIA carga
    # todo (incluyendo media/font) y eso es parte de lo que hace que
    # Amazon no lo detecte como bot. Bloquear recursos cambia el patrón
    # de network requests y es una señal extra de automation.
    block_resource_types: tuple[str, ...] = ()
    capture_screenshot_on_failure: bool = True

    # ----- Anti-detección heredada del legacy AmazonScrapperIA -----
    # Visitar Amazon homepage al inicio de la sesión para establecer
    # cookies y parecer un browser legítimo. Default off para no ralentizar
    # tests en CI; el orchestrator lo activa explícitamente para Amazon.
    warmup_amazon_homepage: bool = False
    # Headers HTTP extra (legacy). Útil para Amazon; ML usa cookies propias.
    extra_http_headers_amazon: dict = field(
        default_factory=lambda: {
            "Accept-Language": "es-MX,es;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "max-age=0",
            "Sec-Ch-Ua": (
                '"Chromium";v="124", "Google Chrome";v="124", '
                '"Not-A.Brand";v="99"'
            ),
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    # Reintento de Amazon en HTTP 503 (legacy: espera 15–30s y reintenta una vez).
    amazon_retry_on_503: bool = True
    amazon_retry_wait_range_seconds: tuple[float, float] = (15.0, 30.0)

    # ----- Sesión persistente (login manual del operador) -----
    # Si está set, Playwright usa `launch_persistent_context` apuntando a
    # este directorio. Las cookies, localStorage, indexedDB, sesiones, etc.
    # persisten entre runs sin necesidad de re-login. El operador puede
    # iniciar sesión manualmente una vez con `python -m ofertas_hunter
    # login --marketplace mercadolibre` y luego correr el bot normal.
    user_data_dir: Optional[str] = None

    def random_user_agent(self) -> str:
        return self.user_agent or random.choice(USER_AGENTS)

    def random_viewport(self) -> dict:
        return self.viewport or random.choice(VIEWPORTS)


@dataclass
class RenderedPage:
    """Página renderizada por el browser worker."""

    url: str
    final_url: str
    status: int
    html: str
    screenshot_bytes: Optional[bytes] = None
    error: Optional[str] = None
    blocked: bool = False
    duration_ms: Optional[int] = None
    extras: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error and not self.blocked and bool(self.html)


@runtime_checkable
class BrowserWorker(Protocol):
    """Interfaz mínima de un browser para fetch de páginas.

    El revalidator (y futuros hunters) usan esta interfaz; los tests inyectan
    un `FakeBrowserWorker`.
    """

    async def fetch(self, url: str) -> RenderedPage: ...
    async def aclose(self) -> None: ...
