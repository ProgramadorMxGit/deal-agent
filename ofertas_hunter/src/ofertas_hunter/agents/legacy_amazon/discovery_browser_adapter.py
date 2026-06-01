"""Adapter para usar el `LegacyAmazonWorker` como `BrowserWorker` para
el `DiscoveryAgent`.

El `DiscoveryAgent` espera un Protocol con `fetch(url) -> RenderedPage`.
El `LegacyAmazonWorker` expone `fetch_product(url) -> LegacyFetchResult`,
diseñado para páginas de producto. Pero con la receta anti-captcha del
legacy también funciona perfectamente para listings/búsquedas
(`/s?k=...`, `/gp/bestsellers/...`).

Este adapter:

- Reusa la receta anti-captcha del legacy (browser efímero + UA random +
  stealth + delays gaussianos).
- Convierte `LegacyFetchResult` a `RenderedPage` que es lo que el
  `DiscoveryAgent` consume.
- Expone también `aclose()` para que el ciclo de vida del browser legacy
  sea gestionado por el orquestador igual que el browser nuevo.

Lo usa el `ServerContext.get_amazon_discovery()` cuando el flag
`amazon_hunter_legacy=True` está activo.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Optional

from ...browser.browser_context import RenderedPage
from .worker import LegacyAmazonWorker, LegacyFetchResult


logger = logging.getLogger(__name__)


class LegacyDiscoveryBrowser:
    """Wrapper que expone `fetch(url)` sobre un `LegacyAmazonWorker`.

    El `DiscoveryAgent` solo necesita `fetch(url) -> RenderedPage` +
    `aclose()`. Este adapter delega en un `LegacyAmazonWorker` interno
    pero traduce el resultado a `RenderedPage`.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        warmup_homepage: bool = True,
        delay_between_requests_ms: tuple[int, int] = (8000, 15000),
    ) -> None:
        self._worker = LegacyAmazonWorker(
            headless=headless,
            warmup_homepage=warmup_homepage,
            delay_between_requests_ms=delay_between_requests_ms,
        )

    async def fetch(self, url: str) -> RenderedPage:
        """Hace fetch usando la receta anti-captcha del legacy.

        Devuelve `RenderedPage`:
        - `ok=True, html=<content>` si extrajo HTML.
        - `ok=False, blocked=True, error='captcha'` si captcha real.
        - `ok=False, blocked=False, error='dom_incomplete'` si el DOM
          está degradado pero no es captcha (criterio C de la spec).
        """
        await self._worker._ensure_started()
        await self._worker._warmup()
        await self._worker._enforce_pacing()

        ok, status = await self._worker._navigate(url)
        page = self._worker._page
        final_url = page.url if page else url

        if not ok:
            return RenderedPage(
                url=url,
                final_url=final_url,
                status=status or 0,
                html="",
                error=f"nav_failed_{status}" if status else "nav_failed",
                blocked=False,
            )

        # Comportamiento humano antes de leer el DOM
        try:
            await self._worker._human_scroll()
            await self._worker._move_mouse_randomly()
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.3, 0.8))

        try:
            content = await page.content()
        except Exception as exc:
            return RenderedPage(
                url=url,
                final_url=final_url,
                status=status,
                html="",
                error=f"content_read_failed: {exc}",
                blocked=False,
            )

        # Detección captcha (misma lógica que `_is_real_captcha` del
        # worker). Importamos local para no acoplar.
        from .worker import _is_real_captcha

        is_captcha, confidence, signals = _is_real_captcha(final_url, content)
        if is_captcha and confidence == "high":
            logger.warning(
                "legacy_discovery: captcha real high-confidence en %s signals=%s",
                url[:80],
                signals,
            )
            return RenderedPage(
                url=url,
                final_url=final_url,
                status=status,
                html="",
                error="captcha",
                blocked=True,
            )
        if is_captcha and confidence == "medium":
            # Sospecha medium: NO marcamos blocked (criterio C). El
            # caller verá `error='captcha_suspect'` pero el agente puede
            # seguir intentando.
            logger.info(
                "legacy_discovery: captcha medium-confidence en %s — sin pausar",
                url[:80],
            )
            return RenderedPage(
                url=url,
                final_url=final_url,
                status=status,
                html="",
                error="captcha_suspect_medium",
                blocked=False,
            )

        return RenderedPage(
            url=url,
            final_url=final_url,
            status=status,
            html=content,
            error=None,
            blocked=False,
        )

    async def aclose(self) -> None:
        try:
            await self._worker.aclose()
        except Exception:
            pass

    # Context manager compatible con `async with` del orquestador.
    async def __aenter__(self) -> "LegacyDiscoveryBrowser":
        await self._worker.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()
