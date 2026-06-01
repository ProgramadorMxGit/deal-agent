"""Extractor del enlace de afiliado de Amazon SiteStripe.

Flujo real validado localmente:

1. Navegar al producto Amazon con una sesión afiliada activa.
2. Confirmar `#nav-AssociateStripe`.
3. Click en `#amzn-ss-get-link-button`.
4. Seleccionar "Enlace corto" en el modal.
5. Click en "Copiar enlace de afiliado".
6. Leer el portapapeles web (`navigator.clipboard.readText()`).

También expone metadatos opcionales del modal: `store_id`, `tracking_id`,
`commission_category` y `commission_rate` cuando estén visibles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


logger = logging.getLogger(__name__)


@dataclass
class AffiliateInfo:
    affiliate_url: Optional[str] = None
    store_id: Optional[str] = None
    tracking_id: Optional[str] = None
    commission_category: Optional[str] = None
    commission_rate: Optional[str] = None
    success: bool = False
    error: Optional[str] = None


@runtime_checkable
class AffiliateExtractor(Protocol):
    async def extract(self, url: str) -> AffiliateInfo: ...
    async def aclose(self) -> None: ...


class PlaywrightAffiliateExtractor:
    def __init__(
        self,
        browser_context,
        *,
        modal_timeout_ms: int = 6000,
        clipboard_wait_ms: int = 2500,
        warmup_wait_ms: int = 3000,
    ) -> None:
        self._context = browser_context
        self.modal_timeout_ms = modal_timeout_ms
        self.clipboard_wait_ms = clipboard_wait_ms
        self.warmup_wait_ms = warmup_wait_ms

    async def extract(self, url: str) -> AffiliateInfo:
        info = AffiliateInfo()
        if self._context is None:
            return AffiliateInfo(success=False, error="no_browser_context")

        page = await self._context.new_page()
        try:
            await self._context.grant_permissions(
                ["clipboard-read", "clipboard-write"],
                origin="https://www.amazon.com.mx",
            )
        except Exception:
            logger.debug("amazon affiliate: no se pudieron otorgar permisos de clipboard")

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(self.warmup_wait_ms)

            stripe = page.locator("#nav-AssociateStripe")
            if await stripe.count() == 0:
                info.error = "sitestripe_not_visible"
                return info

            info.commission_category = await _text_or_none(page, "#amzn-ss-category-content")
            info.commission_rate = await _text_or_none(page, "#amzn-ss-commission-rate-content")
            info.store_id = await _select_value_or_none(page, "#amzn-ss-store-id-dropdown-text")
            info.tracking_id = await _select_value_or_none(
                page, "#amzn-ss-tracking-id-dropdown-text"
            )

            get_link_btn = page.locator("#amzn-ss-get-link-button")
            await get_link_btn.click(timeout=10000)

            modal = page.locator('.a-popover[aria-hidden="false"]').first
            if await modal.count() == 0:
                await page.wait_for_timeout(self.modal_timeout_ms)
            if await modal.count() == 0:
                info.error = "affiliate_modal_not_open"
                return info

            short_radio = page.locator("#amzn-ss-short-link-radio-button input[type=radio]")
            if await short_radio.count() > 0:
                try:
                    await short_radio.check()
                    await page.wait_for_timeout(500)
                except Exception:
                    logger.debug("amazon affiliate: no se pudo forzar short link")

            copy_btn = page.locator("#amzn-ss-copy-affiliate-link-btn-announce")
            if await copy_btn.count() == 0:
                info.error = "copy_affiliate_button_not_visible"
                return info
            await copy_btn.click(timeout=10000)
            await page.wait_for_timeout(self.clipboard_wait_ms)

            try:
                clipboard = await page.evaluate(
                    "async () => await navigator.clipboard.readText()"
                )
            except Exception as exc:
                info.error = f"clipboard_read_failed: {exc}"
                return info

            clipboard_text = (clipboard or "").strip()
            if not clipboard_text:
                info.error = "clipboard_empty"
                return info

            if "amzn.to/" not in clipboard_text and "amazon.com.mx" not in clipboard_text:
                info.error = "affiliate_link_not_detected"
                return info

            info.affiliate_url = clipboard_text
            info.success = True
            # Intentar releer store/tracking ya dentro del modal.
            info.store_id = info.store_id or await _select_value_or_none(
                page, "#amzn-ss-store-id-dropdown-text"
            )
            info.tracking_id = info.tracking_id or await _select_value_or_none(
                page, "#amzn-ss-tracking-id-dropdown-text"
            )
            return info

        except Exception as exc:
            logger.warning("amazon affiliate extractor failed for %s: %s", url, exc)
            info.error = f"unexpected: {exc}"
            return info
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def aclose(self) -> None:
        return


async def _text_or_none(page, selector: str) -> Optional[str]:
    try:
        text = (await page.locator(selector).inner_text()).strip()
    except Exception:
        return None
    return text or None


async def _select_value_or_none(page, selector: str) -> Optional[str]:
    try:
        value = (await page.locator(selector).input_value()).strip()
    except Exception:
        return None
    return value or None
