"""Extractor del enlace de afiliado de Mercado Libre.

Reproduce la lógica de `bot_diversidad_global/src/browser_worker.extract_affiliate_link`:

1. Navegar a la página del producto con cookies de un usuario afiliado.
2. (Opcional) Leer texto de comisión `.stripe-commission__info span`.
3. Click en `[data-testid="generate_link_button"]`.
4. Esperar el modal con texto "Generar link".
5. Polling sobre `[data-testid="text-field__label_link"]` hasta que tenga
   valor (la URL `meli.la/...`).
6. Polling sobre `[data-testid="text-field__label_id"]` (id afiliado).

El extractor está **detrás de un Protocol** (`AffiliateExtractor`) para que
los tests usen un fake. La implementación real (`PlaywrightAffiliateExtractor`)
importa Playwright lazy.

Campos relevantes en el `AffiliateInfo`:

- `affiliate_url`: `https://meli.la/...` para publicación.
- `affiliate_product_id`: id corto tipo `YRYYQB-9DAH`.
- `commission_text`: ej `"COMISIÓN 9%"` (informativo).
- `error`: motivo de falla si `success=False`.

Ningún método lanza: si falla, devuelve `success=False` con `error`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


logger = logging.getLogger(__name__)


@dataclass
class AffiliateInfo:
    """Resultado de la extracción del modal Compartir."""

    affiliate_url: Optional[str] = None
    affiliate_product_id: Optional[str] = None
    commission_text: Optional[str] = None
    success: bool = False
    error: Optional[str] = None


@runtime_checkable
class AffiliateExtractor(Protocol):
    """Interfaz mínima usada por el hunter ML."""

    async def extract(self, url: str) -> AffiliateInfo: ...
    async def aclose(self) -> None: ...


# ---------------------------------------------------------------------------
# Implementación Playwright real
# ---------------------------------------------------------------------------


class PlaywrightAffiliateExtractor:
    """Implementación real con Playwright.

    Recibe un `BrowserContext` ya iniciado con cookies (el caller es
    responsable de cargar cookies vía `MercadoLibreSession`). Esto es un
    objeto de Playwright; mantenemos la dependencia detrás de import lazy.
    """

    def __init__(
        self,
        browser_context,
        *,
        modal_timeout_ms: int = 6000,
        poll_iterations: int = 10,
        poll_interval_ms: int = 500,
        warmup_wait_ms: int = 2000,
    ) -> None:
        self._context = browser_context
        self.modal_timeout_ms = modal_timeout_ms
        self.poll_iterations = poll_iterations
        self.poll_interval_ms = poll_interval_ms
        self.warmup_wait_ms = warmup_wait_ms

    async def extract(self, url: str) -> AffiliateInfo:
        """Navega al producto, abre modal, extrae link e id afiliado."""
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeout  # type: ignore
        except ImportError:
            return AffiliateInfo(success=False, error="playwright_not_installed")

        info = AffiliateInfo()
        if self._context is None:
            return AffiliateInfo(success=False, error="no_browser_context")

        page = await self._context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(self.warmup_wait_ms)

            # Comisión (informativo, no bloquea si no aparece).
            try:
                # Intentar varios selectores — ML cambia la estructura
                for commission_sel in [
                    ".stripe-commission__info span",
                    ".toolbar__commission",
                    "nav[aria-label='Afiliados'] .andes-typography",
                    ".stripe .andes-typography",
                ]:
                    try:
                        commission = await page.locator(commission_sel).first.text_content(timeout=2000)
                        if commission and ("%" in commission or "comisi" in commission.lower()):
                            info.commission_text = commission.strip()
                            break
                    except PlaywrightTimeout:
                        continue
            except Exception as exc:
                logger.debug("commission text not extracted: %s", exc)

            # Click Compartir — el botón puede estar en la barra de afiliados (nav.stripe)
            # o dentro del contenido del producto
            btn = page.locator('[data-testid="generate_link_button"]')
            try:
                await btn.wait_for(state="visible", timeout=5000)
            except PlaywrightTimeout:
                # Intentar scroll al top donde está la barra de afiliados
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(1000)
                try:
                    await btn.wait_for(state="visible", timeout=3000)
                except PlaywrightTimeout:
                    # Último intento: buscar por texto "Compartir" en cualquier botón
                    btn_alt = page.locator("button:has-text('Compartir')").first
                    try:
                        await btn_alt.wait_for(state="visible", timeout=3000)
                        btn = btn_alt
                    except PlaywrightTimeout:
                        info.error = "share_button_not_visible"
                        return info

            await btn.click()

            # Esperar modal abierto — ML puede mostrar "Generar link" o "Compartir"
            modal_opened = False
            for modal_text in ["Generar link", "Compartir", "link"]:
                try:
                    await page.wait_for_selector(
                        f"text={modal_text}", timeout=3000
                    )
                    modal_opened = True
                    break
                except PlaywrightTimeout:
                    continue

            # También intentar esperar directamente el textarea del link
            if not modal_opened:
                try:
                    await page.wait_for_selector(
                        '[data-testid="text-field__label_link"]',
                        timeout=self.modal_timeout_ms,
                    )
                    modal_opened = True
                except PlaywrightTimeout:
                    pass

            if not modal_opened:
                info.error = "modal_did_not_open"
                return info

            # Polling por affiliate_url y product_id (poblados por JS).
            link_textarea = page.locator('[data-testid="text-field__label_link"]')
            id_textarea = page.locator('[data-testid="text-field__label_id"]')

            for _ in range(self.poll_iterations):
                await page.wait_for_timeout(self.poll_interval_ms)
                if info.affiliate_url is None and await link_textarea.count() > 0:
                    val = await self._read_textarea(link_textarea)
                    if val:
                        info.affiliate_url = val
                if info.affiliate_product_id is None and await id_textarea.count() > 0:
                    val = await self._read_textarea(id_textarea)
                    if val:
                        info.affiliate_product_id = val
                if info.affiliate_url and info.affiliate_product_id:
                    break

            if info.affiliate_url or info.affiliate_product_id:
                info.success = True
            else:
                info.error = "modal_textareas_empty"
            return info

        except Exception as exc:
            logger.warning("affiliate extractor failed for %s: %s", url, exc)
            info.error = f"unexpected: {exc}"
            return info
        finally:
            try:
                await page.close()
            except Exception:
                pass

    @staticmethod
    async def _read_textarea(locator) -> Optional[str]:
        try:
            val = await locator.input_value(timeout=1000)
        except Exception:
            try:
                val = await locator.text_content() or ""
            except Exception:
                return None
        val = (val or "").strip()
        return val or None

    async def aclose(self) -> None:
        # El context se cierra por el caller (es compartido).
        return
