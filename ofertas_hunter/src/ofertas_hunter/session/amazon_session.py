"""Sesion de Amazon.

Wrapper simple para:

- resolver el archivo de cookies Amazon desde settings/env;
- cargar cookies normalizadas con ``CookieStore``;
- inyectarlas en un browser Playwright ya iniciado.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .cookie_store import CookieHealth, CookieStore, resolve_cookie_path


logger = logging.getLogger(__name__)


@dataclass
class AmazonSession:
    cookies_path: Path
    expiry_warn_seconds: int = 7 * 24 * 3600

    @classmethod
    def from_settings(
        cls,
        *,
        cookies_path: Optional[str] = None,
        legacy_env_vars: tuple[str, ...] = ("AMAZON_COOKIES_PATH",),
    ) -> "AmazonSession":
        resolved = resolve_cookie_path(
            primary=cookies_path,
            env_vars=legacy_env_vars,
            default="secrets/amazon_cookies.json",
        )
        return cls(cookies_path=resolved)

    def store(self) -> CookieStore:
        return CookieStore(
            path=self.cookies_path,
            expiry_warn_seconds=self.expiry_warn_seconds,
        )

    def load(self) -> tuple[list[dict], CookieHealth]:
        return self.store().load()

    def save(self, cookies: list[dict]) -> Path:
        return self.store().save(cookies)

    async def inject_into_browser(self, browser) -> CookieHealth:
        cookies, health = self.load()
        if not cookies:
            return health

        await browser._ensure_started()  # noqa: SLF001
        ctx = getattr(browser, "_context", None)
        if ctx is None:
            logger.warning("Amazon cookies: browser._context es None")
            return CookieHealth(
                path=health.path,
                loaded=health.loaded,
                total_in_file=health.total_in_file,
                is_missing=health.is_missing,
                is_empty=health.is_empty,
                expiring_soon=health.expiring_soon,
                error="browser_context_missing",
            )

        await ctx.add_cookies(cookies)
        logger.info("Amazon cookies: %d cookies cargadas en el contexto", len(cookies))
        return health
