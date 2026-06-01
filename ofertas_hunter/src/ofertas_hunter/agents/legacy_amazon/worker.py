"""Worker minimal anti-captcha para Amazon.com.mx, derivado del legacy
`AmazonScrapperIA/src/browser_worker.py`.

Diferencias vs. el legacy:

- NO mantiene frontier propio: la URL viene del frontier compartido del bot
  nuevo (pasada por argumento).
- NO maneja sesiones largas (`run_session`): expone `fetch_product(url)`
  para una sola URL por llamada.
- NO escribe `data/offers.json` / `data/state.json`: el caller persiste en
  SQLite via `LegacyAmazonAdapter`.
- Sí conserva la receta anti-captcha: browser efímero, UA + viewport
  random, headers completos, stealth script, delays gaussianos, scroll
  humano, mouse aleatorio, retry suave en 503.
- Sí detecta captchas (tres niveles: URL signal, contenido amazon-captcha,
  estructura del DOM) y emite resultado tipado para que el caller decida
  pausar marketplace o no.

El método público `fetch_product(url)` retorna `LegacyFetchResult` con:

- `ok`: bool — la página cargó y se extrajo `price_current` y `title`.
- `captcha`: bool — la página final es captcha real (alta confianza).
- `data`: dict | None — diccionario crudo del extractor legacy.
- `final_url`: URL final tras redirects.
- `status`: status HTTP (200/503/etc).
- `reason`: texto si falló (`captcha`, `503`, `dom_incomplete`,
  `extract_failed`).

NO clasifica como captcha cuando solo falta `price_current` o el DOM está
incompleto: distingue captcha real vs DOM degradado para evitar pausas
falsas (criterio C de la spec).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .price_parser import (
    extract_product_data_from_page,
    extract_products_from_search,
)


logger = logging.getLogger(__name__)


AMAZON_BASE = "https://www.amazon.com.mx"


# -------------------------------------------------------------------------
# Fingerprints rotables (idéntico al legacy)
# -------------------------------------------------------------------------


USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.207 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.122 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.128 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.207 Safari/537.36 Edg/124.0.2478.109",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.207 Safari/537.36",
)


VIEWPORTS = (
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1280, "height": 720},
)


STEALTH_SCRIPT = """
() => {
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const arr = [
                {name:'Chrome PDF Plugin', filename:'internal-pdf-viewer'},
                {name:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
                {name:'Native Client', filename:'internal-nacl-plugin'},
            ];
            arr.__proto__ = PluginArray.prototype;
            return arr;
        }
    });
    Object.defineProperty(navigator, 'languages', {get: () => ['es-MX', 'es', 'en-US', 'en']});
    window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
        Promise.resolve({state: Notification.permission}) :
        originalQuery(parameters)
    );
    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
    Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
    Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
    Object.defineProperty(navigator, 'vendor', {get: () => 'Google Inc.'});
}
"""


# -------------------------------------------------------------------------
# Resultado tipado del fetch (para el adapter)
# -------------------------------------------------------------------------


@dataclass
class LegacyFetchResult:
    ok: bool
    final_url: str
    status: int
    captcha: bool
    captcha_confidence: Optional[str]  # "high" si la URL está en validateCaptcha
    data: Optional[dict[str, Any]]
    reason: Optional[str]  # captcha | 503 | dom_incomplete | extract_failed | nav_failed
    raw_html: Optional[str] = None  # para diagnóstico (limitado a 4KB)
    captcha_signals: tuple[str, ...] = field(default_factory=tuple)


# -------------------------------------------------------------------------
# Helpers locales
# -------------------------------------------------------------------------


def _is_captcha_url(url: str) -> bool:
    """True si la URL final es la página captcha de Amazon (señal fuerte)."""
    if not url:
        return False
    return any(
        s in url
        for s in (
            "/errors/validateCaptcha",
            "/captcha/",
            "/cs/help/contact-us",  # a veces redirige cuando bloquea
        )
    )


def _captcha_signals_in_content(content: str) -> list[str]:
    """Detecta señales DURAS de captcha en el HTML de la página.

    Distinción vs. detector del bot nuevo: aquí no marcamos captcha por
    "DOM sin precio" (eso lo evita el criterio C de la spec). Sólo
    señales reales: form action validateCaptcha, input captchacharacters,
    y dos textos largos canónicos.
    """
    if not content:
        return []
    content_lower = content.lower()
    signals: list[str] = []

    # Form action = validateCaptcha → indicador más fuerte y barato
    if 'action="/errors/validatecaptcha"' in content_lower:
        signals.append("form_action_validate_captcha")
    if 'name="amzn-captcha-' in content_lower:
        signals.append("input_amzn_captcha")
    if 'id="captchacharacters"' in content_lower:
        signals.append("input_captchacharacters")

    # Textos canonical (legacy detector). Lo conservamos pero como
    # señal complementaria, no decisoria.
    canonical_texts = (
        "enter the characters you see",
        "escribe los caracteres que ves",
        "type the characters you see",
    )
    for t in canonical_texts:
        if t in content_lower:
            signals.append(f"text:{t[:25]}")
            break

    return signals


def _is_real_captcha(final_url: str, content: str) -> tuple[bool, str, list[str]]:
    """Decide si la página es captcha real con confianza.

    Retorna (is_captcha, confidence, signals).
    - confidence='high' si la URL final es validateCaptcha o si hay 2+
      señales fuertes (form_action + input).
    - confidence='medium' si hay 1 señal fuerte sola (puede ser falso).
    - de lo contrario False.
    """
    signals = _captcha_signals_in_content(content or "")
    url_signal = _is_captcha_url(final_url or "")

    if url_signal:
        return True, "high", ["captcha_url", *signals]

    strong = [
        s for s in signals
        if s in ("form_action_validate_captcha", "input_amzn_captcha", "input_captchacharacters")
    ]
    if len(strong) >= 2:
        return True, "high", signals
    if len(strong) == 1:
        # Una señal sola no es definitiva: el legacy distinguía esto de
        # "DOM degradado" tras 503. Marcamos medium para que el caller
        # decida pausar o no según política.
        return True, "medium", signals
    return False, "none", signals


# -------------------------------------------------------------------------
# Worker minimal
# -------------------------------------------------------------------------


class LegacyAmazonWorker:
    """Worker liviano que reproduce la receta anti-captcha del legacy.

    Uso:
        worker = LegacyAmazonWorker(headless=True)
        async with worker:
            result = await worker.fetch_product(url)

    `result` es `LegacyFetchResult`. Si `result.ok` y `result.data`
    están poblados, el adapter puede convertir a `Product+Offer`.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        warmup_homepage: bool = True,
        delay_between_requests_ms: tuple[int, int] = (8000, 15000),
        max_pages: int = 30,
    ) -> None:
        self.headless = headless
        self.warmup_homepage = warmup_homepage
        self.delay_between_requests_ms = delay_between_requests_ms
        self.max_pages = max_pages
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._pages_done = 0
        self._last_fetch_at: float = 0.0
        self._warmed_up = False

    async def __aenter__(self) -> "LegacyAmazonWorker":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        ua = random.choice(USER_AGENTS)
        vp = random.choice(VIEWPORTS)

        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
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
            ],
        )
        self._context = await self._browser.new_context(
            user_agent=ua,
            viewport=vp,
            locale="es-MX",
            timezone_id="America/Mexico_City",
            extra_http_headers={
                "Accept-Language": "es-MX,es;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "max-age=0",
                "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        await self._context.add_init_script(STEALTH_SCRIPT)
        self._page = await self._context.new_page()
        logger.info(
            "legacy_amazon_worker started: ua=%s vp=%dx%d headless=%s",
            ua[:60],
            vp["width"],
            vp["height"],
            self.headless,
        )

    async def aclose(self) -> None:
        try:
            if self._page is not None:
                await self._page.close()
        except Exception:
            pass
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
        self._playwright = self._browser = self._context = self._page = None

    async def _ensure_started(self) -> None:
        if self._page is None:
            await self.start()

    async def _human_delay(self, min_s: float = 2.0, max_s: float = 5.0) -> None:
        # Distribución normal truncada (legacy)
        base = random.gauss((min_s + max_s) / 2, (max_s - min_s) / 4)
        delay = max(min_s, min(max_s, base))
        await asyncio.sleep(delay)

    async def _human_scroll(self) -> None:
        try:
            height = await self._page.evaluate("document.body.scrollHeight")
            current = 0
            while current < min(height, 3000):
                step = random.randint(200, 500)
                current += step
                await self._page.evaluate(f"window.scrollTo(0, {current})")
                await asyncio.sleep(random.uniform(0.1, 0.4))
            if random.random() > 0.5:
                await self._page.evaluate(
                    f"window.scrollTo(0, {random.randint(100, 400)})"
                )
                await asyncio.sleep(random.uniform(0.2, 0.5))
        except Exception:
            pass

    async def _move_mouse_randomly(self) -> None:
        try:
            vp = self._page.viewport_size or {"width": 1366, "height": 768}
            for _ in range(random.randint(2, 4)):
                x = random.randint(100, vp["width"] - 100)
                y = random.randint(100, vp["height"] - 100)
                await self._page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.05, 0.2))
        except Exception:
            pass

    async def _warmup(self) -> None:
        """Visita la home Amazon antes del primer fetch (legacy receta)."""
        if self._warmed_up or not self.warmup_homepage:
            return
        try:
            await self._page.goto(
                AMAZON_BASE, wait_until="domcontentloaded", timeout=20000
            )
            await asyncio.sleep(random.uniform(1.5, 3.0))
            await self._human_scroll()
            await self._move_mouse_randomly()
            self._warmed_up = True
            logger.debug("legacy_amazon_worker warmed up homepage")
        except Exception as exc:
            logger.debug("warmup failed (non-fatal): %s", exc)

    async def _navigate(self, url: str, *, timeout_ms: int = 30000) -> tuple[bool, int]:
        """Navega con retry suave en 503 y respeto a límites del bot."""
        try:
            resp = await self._page.goto(
                url, wait_until="domcontentloaded", timeout=timeout_ms
            )
            if resp is None:
                return False, 0
            status = resp.status
            if status == 200 or status in (301, 302):
                await asyncio.sleep(random.uniform(0.8, 1.5))
                return True, status
            if status == 503:
                wait = random.uniform(15, 30)
                logger.warning(
                    "amazon 503 en %s — esperando %.0fs (legacy retry)", url[:60], wait
                )
                await asyncio.sleep(wait)
                try:
                    resp2 = await self._page.goto(
                        url, wait_until="domcontentloaded", timeout=timeout_ms
                    )
                    if resp2 and resp2.status == 200:
                        await asyncio.sleep(random.uniform(1, 2))
                        return True, 200
                except Exception:
                    pass
                return False, 503
            return False, status
        except Exception as exc:
            logger.debug("nav error %s: %s", url[:60], exc)
            return False, 0

    async def _enforce_pacing(self) -> None:
        """Respeta delay mínimo entre requests."""
        if self._last_fetch_at == 0.0:
            self._last_fetch_at = time.monotonic()
            return
        elapsed_ms = (time.monotonic() - self._last_fetch_at) * 1000
        min_ms, max_ms = self.delay_between_requests_ms
        target_ms = random.randint(min_ms, max_ms)
        if elapsed_ms < target_ms:
            await asyncio.sleep((target_ms - elapsed_ms) / 1000)
        self._last_fetch_at = time.monotonic()

    async def fetch_product(self, url: str) -> LegacyFetchResult:
        """Procesa una URL Amazon y devuelve `LegacyFetchResult`."""
        await self._ensure_started()
        await self._warmup()
        await self._enforce_pacing()

        ok, status = await self._navigate(url)
        final_url = self._page.url if self._page else url

        if not ok:
            reason = f"nav_failed_{status}" if status else "nav_failed"
            return LegacyFetchResult(
                ok=False,
                final_url=final_url,
                status=status,
                captcha=False,
                captcha_confidence=None,
                data=None,
                reason=reason,
            )

        # Comportamiento humano antes de leer DOM
        await self._human_scroll()
        await self._move_mouse_randomly()
        await asyncio.sleep(random.uniform(0.3, 0.8))

        # Leer contenido para detección captcha + extracción
        try:
            content = await self._page.content()
        except Exception as exc:
            return LegacyFetchResult(
                ok=False,
                final_url=final_url,
                status=status,
                captcha=False,
                captcha_confidence=None,
                data=None,
                reason=f"content_read_failed: {exc}",
            )

        is_captcha, confidence, signals = _is_real_captcha(final_url, content)
        if is_captcha:
            return LegacyFetchResult(
                ok=False,
                final_url=final_url,
                status=status,
                captcha=True,
                captcha_confidence=confidence,
                data=None,
                reason="captcha",
                raw_html=content[:4000],
                captcha_signals=tuple(signals),
            )

        # Extraer producto
        try:
            data = await extract_product_data_from_page(self._page, url)
        except Exception as exc:
            return LegacyFetchResult(
                ok=False,
                final_url=final_url,
                status=status,
                captcha=False,
                captcha_confidence=None,
                data=None,
                reason=f"extract_failed: {exc}",
                raw_html=content[:4000],
            )

        # DOM incompleto / extracción parcial: NO es captcha. El adapter
        # decide qué hacer con campos faltantes.
        has_title = bool((data or {}).get("title"))
        has_price = (data or {}).get("price_current") is not None
        if not has_title or not has_price:
            return LegacyFetchResult(
                ok=False,
                final_url=final_url,
                status=status,
                captcha=False,
                captcha_confidence=None,
                data=data,
                reason="dom_incomplete",
                raw_html=content[:4000],
            )

        self._pages_done += 1
        return LegacyFetchResult(
            ok=True,
            final_url=final_url,
            status=status,
            captcha=False,
            captcha_confidence=None,
            data=data,
            reason=None,
        )
