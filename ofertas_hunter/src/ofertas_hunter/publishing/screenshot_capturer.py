"""Captura de screenshot de la ficha de producto (PDP) para publicar en WhatsApp.

En lugar de mandar al grupo la imagen pública del catálogo (`image_url`), el bot
puede mandar un *screenshot recortado* de la zona superior de la página real del
producto: imagen + título + precio + caja de compra. Eso da una foto más rica y
con contexto (precio tachado, descuento, vendedor).

Diseño:

- `ScreenshotCapturer` gestiona su PROPIO navegador Playwright (headless),
  separado del worker del revalidator, para no interferir con la página
  persistente que éste reutiliza.
- Inyecta cookies de Mercado Libre y Amazon en un único contexto con viewport
  desktop fijo (1366x900). Eso fuerza el layout limpio de 2-3 columnas.
- Para Mercado Libre, reescribe el host `articulo.mercadolibre.com.mx` →
  `www.mercadolibre.com.mx` en las URLs de catálogo (`/p/MLM...`), porque el
  subdominio `articulo.` devuelve 404 en esas URLs.
- Toma el recorte uniendo las columnas clave (estrategia del scraper legacy
  `_capture_detail_section`). Si falla, cae a un contenedor; si todo falla,
  devuelve `None` y el publisher usa la imagen pública como fallback.

Es **best-effort**: cualquier excepción (captcha, timeout, Playwright ausente)
se traga y devuelve `None`. Nunca debe tumbar la publicación.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse


logger = logging.getLogger(__name__)


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.6367.207 Safari/537.36"
)

_VIEWPORT = {"width": 1366, "height": 900}

# Recorte máximo (px). Espejo de la lógica del legacy, ajustado al viewport.
_MAX_SECTION_WIDTH = 1200.0
_MAX_SECTION_HEIGHT = 980.0
_PAD = 16

# JS que oculta coachmarks/onboarding/tooltips que se superponen al contenido
# (Mercado Libre: "Haz tu primera compra mayorista" con precios por unidad;
# también react-floater, andes-popper, etc.). Ocultar es más robusto que
# clickear el botón "Cerrar" (no depende de timing ni de interceptación).
_DISMISS_OVERLAYS_JS = r"""() => {
  const selectors = [
    '.andes-coach-marks',
    '[class*="coach-mark"]',
    '[class*="coachmark"]',
    '.__floater',
    '[class*="__floater"]',
    '.andes-popper',
    '[data-testid="coachmark"]',
    '[class*="onboarding"]',
  ];
  let hidden = 0;
  for (const s of selectors) {
    document.querySelectorAll(s).forEach((el) => {
      try { el.style.setProperty('display', 'none', 'important'); hidden++; } catch (e) {}
    });
  }
  return hidden;
}"""

# Selectores por marketplace: columnas a unir (recorte preferido), contenedores
# fallback, selector de título a esperar y banners de cookies a aceptar.
_MARKETPLACE_SELECTORS: dict[str, dict[str, Any]] = {
    "amazon": {
        "columns": ["#leftCol", "#centerCol", "#rightCol"],
        "containers": ["#dp-container", "#ppd", "#dp"],
        "title_wait": "#productTitle",
        "cookie_accept": [
            "#sp-cc-accept",
            "input#sp-cc-accept",
            "button[name='accept']",
        ],
    },
    "mercadolibre": {
        "columns": [
            ".ui-pdp-gallery",
            ".ui-pdp-title",
            ".ui-pdp-price",
            ".ui-pdp-buybox",
        ],
        "containers": [".ui-pdp-container__row", ".ui-pdp-container"],
        "title_wait": "h1.ui-pdp-title",
        "cookie_accept": [
            "button[data-testid='action:understood-button']",
            "button.cookie-consent-banner-opt-out__action",
        ],
    },
}


def normalize_ml_pdp_url(url: str) -> str:
    """Reescribe `articulo.mercadolibre.com.mx` → `www.mercadolibre.com.mx`.

    Sólo para URLs de catálogo (`/p/MLM...`), donde el subdominio `articulo.`
    devuelve 404. El resto de URLs se devuelven sin cambios.
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    host = (parsed.netloc or "").lower()
    if host == "articulo.mercadolibre.com.mx" and "/p/" in (parsed.path or ""):
        return urlunparse(parsed._replace(netloc="www.mercadolibre.com.mx"))
    return url


def resolve_capture_url(payload: dict) -> Optional[str]:
    """Elige la mejor URL navegable para capturar el PDP.

    - Mercado Libre: usa `canonical_url` (reescrita a `www.`). NUNCA el
      `affiliate_url`/`url` porque son enlaces cortos `meli.la` que redirigen
      al perfil social del afiliado, no al producto.
    - Amazon / otros: prioriza `canonical_url`, luego `resolved_url`, `url`.
    """
    marketplace = (payload.get("marketplace") or "").lower()
    if marketplace == "mercadolibre":
        canonical = payload.get("canonical_url")
        if canonical:
            return normalize_ml_pdp_url(canonical)
        # Fallback: si por alguna razón no hay canonical, intentamos url/resolved
        # pero sólo si NO es un enlace corto meli.la.
        for key in ("resolved_url", "url"):
            value = payload.get(key)
            if value and "meli.la" not in value:
                return value
        return None

    for key in ("canonical_url", "resolved_url", "url"):
        value = payload.get(key)
        if value:
            return value
    return None


class ScreenshotCapturer:
    """Captura best-effort de la ficha de producto como JPEG bytes."""

    def __init__(
        self,
        *,
        mercadolibre_cookies_path: Optional[str] = None,
        mercadolibre_cookies_fallback_path: Optional[str] = None,
        amazon_cookies_path: Optional[str] = None,
        headless: bool = True,
        nav_timeout_ms: int = 30000,
        jpeg_quality: int = 85,
        enabled: bool = True,
    ) -> None:
        self.mercadolibre_cookies_path = mercadolibre_cookies_path
        self.mercadolibre_cookies_fallback_path = mercadolibre_cookies_fallback_path
        self.amazon_cookies_path = amazon_cookies_path
        self.headless = headless
        self.nav_timeout_ms = nav_timeout_ms
        self.jpeg_quality = jpeg_quality
        self.enabled = enabled

        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._lock = asyncio.Lock()
        # Si Playwright no está disponible, desactivamos para no reintentar.
        self._unavailable = False
        # Contador de fallos de arranque consecutivos. Sólo tras varios
        # seguidos se desactiva la feature (evita apagado permanente por un
        # hipo transitorio). Se resetea al primer arranque exitoso.
        self._launch_failures = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _ensure_started(self) -> bool:
        """Arranca el navegador y carga cookies. Devuelve False si no se pudo."""
        if self._unavailable:
            return False
        # Si el contexto existe pero el browser murió (crash de Chromium,
        # OOM, etc.), reconstruimos. `is_connected()` es la señal fiable.
        if self._context is not None:
            browser = self._browser
            try:
                if browser is not None and not browser.is_connected():
                    logger.warning(
                        "ScreenshotCapturer: browser desconectado; reconstruyendo"
                    )
                    await self._teardown()
                else:
                    return True
            except Exception:
                await self._teardown()
        if self._context is not None:
            return True
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except Exception as exc:
            logger.warning("ScreenshotCapturer: Playwright no disponible (%s)", exc)
            self._unavailable = True
            return False

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    f"--window-size={_VIEWPORT['width']},{_VIEWPORT['height']}",
                ],
                ignore_default_args=["--enable-automation"],
            )
            self._context = await self._browser.new_context(
                user_agent=_USER_AGENT,
                locale="es-MX",
                timezone_id="America/Mexico_City",
                viewport=_VIEWPORT,
                extra_http_headers={
                    "Accept-Language": "es-MX,es;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,image/apng,*/*;q=0.8"
                    ),
                },
            )
            await self._context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            await self._inject_cookies()
            self._launch_failures = 0
            logger.info("ScreenshotCapturer iniciado (headless=%s)", self.headless)
            return True
        except Exception as exc:
            # NO latcheamos `_unavailable` para fallos transitorios de arranque
            # (OOM puntual, race). Sólo tras varios intentos seguidos lo
            # desactivamos para no reintentar en cada publicación. Así un
            # hipo transitorio no apaga la feature para toda la vida del bot.
            self._launch_failures += 1
            logger.warning(
                "ScreenshotCapturer: arranque falló (intento %d): %s",
                self._launch_failures,
                exc,
            )
            await self._teardown()
            if self._launch_failures >= 5:
                logger.error(
                    "ScreenshotCapturer: %d fallos de arranque seguidos; "
                    "desactivando hasta reinicio",
                    self._launch_failures,
                )
                self._unavailable = True
            return False

    async def _inject_cookies(self) -> None:
        """Carga cookies de ML y Amazon en el contexto (best-effort)."""
        if self._context is None:
            return
        # Mercado Libre
        try:
            from ..session.mercadolibre_session import MercadoLibreSession

            ml = MercadoLibreSession.from_settings(
                cookies_path=self.mercadolibre_cookies_path,
                fallback_path=self.mercadolibre_cookies_fallback_path,
            )
            cookies, _health = ml.load()
            if cookies:
                await self._context.add_cookies(cookies)
                logger.info("ScreenshotCapturer: %d cookies ML cargadas", len(cookies))
        except Exception as exc:
            logger.warning("ScreenshotCapturer: cookies ML no cargadas (%s)", exc)
        # Amazon
        try:
            from ..session.amazon_session import AmazonSession

            az = AmazonSession.from_settings(cookies_path=self.amazon_cookies_path)
            cookies, _health = az.load()
            if cookies:
                await self._context.add_cookies(cookies)
                logger.info("ScreenshotCapturer: %d cookies Amazon cargadas", len(cookies))
        except Exception as exc:
            logger.warning("ScreenshotCapturer: cookies Amazon no cargadas (%s)", exc)

    async def _teardown(self) -> None:
        """Cierra browser/context/playwright actuales (sin desactivar la
        feature). Permite reconstruir en el próximo `_ensure_started`."""
        for closer in (
            lambda: self._context.close() if self._context else None,
            lambda: self._browser.close() if self._browser else None,
            lambda: self._playwright.stop() if self._playwright else None,
        ):
            try:
                result = closer()
                if result is not None:
                    await result
            except Exception:
                pass
        self._context = None
        self._browser = None
        self._playwright = None
        self._page = None

    async def aclose(self) -> None:
        await self._teardown()

    # ------------------------------------------------------------------
    # Captura
    # ------------------------------------------------------------------

    async def capture(self, payload: dict) -> Optional[bytes]:
        """Devuelve JPEG bytes del PDP, o `None` si no se pudo capturar.

        Best-effort: cualquier fallo (captcha, timeout, sin URL) devuelve None.
        """
        if not self.enabled or self._unavailable:
            return None

        marketplace = (payload.get("marketplace") or "").lower()
        if marketplace not in _MARKETPLACE_SELECTORS:
            return None
        url = resolve_capture_url(payload)
        if not url:
            logger.info("ScreenshotCapturer: sin URL navegable para %s", marketplace)
            return None

        async with self._lock:
            if not await self._ensure_started():
                return None
            page = None
            try:
                # Página FRESCA por captura: evita que una sola página de vida
                # larga se quede "trabada" (interstitial/modal/nav colgada) y
                # haga fallar todas las capturas siguientes hasta reiniciar.
                page = await self._context.new_page()
                return await self._capture_locked(page, marketplace, url)
            except Exception as exc:
                logger.warning(
                    "ScreenshotCapturer: captura falló marketplace=%s url=%s (%s)",
                    marketplace,
                    url[:80],
                    exc,
                )
                # Si el browser murió, teardown para reconstruir en la próxima.
                try:
                    browser = self._browser
                    if browser is not None and not browser.is_connected():
                        await self._teardown()
                except Exception:
                    await self._teardown()
                return None
            finally:
                # Cerrar la página de esta captura SIEMPRE (no acumular estado).
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass

    async def _capture_locked(self, page: Any, marketplace: str, url: str) -> Optional[bytes]:
        sel = _MARKETPLACE_SELECTORS[marketplace]

        await page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)

        # Aceptar banner de cookies si aparece.
        for selector in sel["cookie_accept"]:
            try:
                loc = page.locator(selector).first
                if await loc.count() > 0:
                    await loc.click(timeout=1200)
                    await page.wait_for_timeout(400)
                    break
            except Exception:
                pass

        # Esperar el título del producto. Si no aparece, probablemente es
        # captcha / página de error → abortamos (fallback a imagen pública).
        try:
            await page.wait_for_selector(sel["title_wait"], timeout=12000)
        except Exception:
            logger.info(
                "ScreenshotCapturer: no apareció %s (posible captcha) en %s",
                sel["title_wait"],
                url[:80],
            )
            return None

        # Cerrar coachmarks/onboarding que ML superpone sobre el precio
        # (p.ej. "Haz tu primera compra mayorista" con la caja de precios por
        # unidad). Se ocultan vía JS porque es más robusto que hacer click
        # (no depende de timing ni de que el botón sea clickeable). Si no se
        # ocultan, la foto engaña mostrando el precio mayorista por unidad.
        try:
            await page.evaluate(_DISMISS_OVERLAYS_JS)
            await page.wait_for_timeout(200)
        except Exception:
            pass

        # Dejar que la galería/JS terminen de asentar el layout.
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        try:
            await page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        await page.wait_for_timeout(700)

        # Segundo pase de cierre: algunos coachmarks aparecen con retraso
        # (tras networkidle / render de la buy box).
        try:
            await page.evaluate(_DISMISS_OVERLAYS_JS)
            await page.wait_for_timeout(150)
        except Exception:
            pass

        return await self._screenshot_detail_section(page, marketplace)

    async def _screenshot_detail_section(
        self, page: Any, marketplace: str
    ) -> Optional[bytes]:
        sel = _MARKETPLACE_SELECTORS[marketplace]
        vp_h = _VIEWPORT["height"]
        shot_opts = {"type": "jpeg", "quality": self.jpeg_quality}

        # 1) Unión de columnas clave visibles en el viewport.
        boxes = []
        for selector in sel["columns"]:
            try:
                loc = page.locator(selector).first
                if await loc.count() == 0:
                    continue
                box = await loc.bounding_box()
            except Exception:
                continue
            if not box or box.get("width", 0) <= 10 or box.get("height", 0) <= 10:
                continue
            # Ignorar elementos que arrancan fuera del viewport (ML "stacked").
            if box["y"] > vp_h:
                continue
            boxes.append(box)

        if boxes:
            min_x = min(b["x"] for b in boxes)
            min_y = min(b["y"] for b in boxes)
            max_x = max(b["x"] + b["width"] for b in boxes)
            max_y = max(b["y"] + b["height"] for b in boxes)
            width = min(max(1.0, (max_x - min_x) + _PAD * 2), _MAX_SECTION_WIDTH)
            height = min(max(1.0, (max_y - min_y) + _PAD * 2), _MAX_SECTION_HEIGHT)
            height = min(height, vp_h - max(0, min_y - _PAD) - 1)
            if height >= 80:  # recorte demasiado bajo = inútil
                clip = {
                    "x": float(max(0, min_x - _PAD)),
                    "y": float(max(0, min_y - _PAD)),
                    "width": float(width),
                    "height": float(max(1.0, height)),
                }
                return await page.screenshot(clip=clip, **shot_opts)

        # 2) Fallback: contenedor principal recortado a la zona superior.
        for selector in sel["containers"]:
            try:
                loc = page.locator(selector).first
                if await loc.count() == 0:
                    continue
                box = await loc.bounding_box()
            except Exception:
                continue
            if not box:
                continue
            height = min(_MAX_SECTION_HEIGHT, max(1.0, box["height"] + _PAD * 2))
            height = min(height, vp_h - max(0, box["y"] - _PAD) - 1)
            if height < 80:
                continue
            clip = {
                "x": float(max(0, box["x"] - _PAD)),
                "y": float(max(0, box["y"] - _PAD)),
                "width": float(min(_MAX_SECTION_WIDTH, max(1.0, box["width"] + _PAD * 2))),
                "height": float(max(1.0, height)),
            }
            return await page.screenshot(clip=clip, **shot_opts)

        # 3) Fallback final: viewport visible.
        return await page.screenshot(**shot_opts)
