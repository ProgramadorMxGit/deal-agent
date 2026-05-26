"""Sesión de Mercado Libre.

Wrapper específico que sabe:

- de qué archivo cargar cookies por defecto;
- cuál es el dominio de aplicación;
- cómo detectar redirecciones de login / verificación.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .cookie_store import CookieStore, CookieHealth, resolve_cookie_path


logger = logging.getLogger(__name__)


_LOGIN_REDIRECT_TOKENS = (
    "/gz/account-verification",
    "/jms/mlm/lgz/login",
    "/account-verification",
    "/login",
    "/auth/security/captcha",
)


@dataclass
class MercadoLibreSession:
    """Carga cookies de ML respetando paths legacy."""

    cookies_path: Path
    browser_cookies_path: Optional[Path] = None
    save_cookies_on_exit: bool = True
    expiry_warn_seconds: int = 7 * 24 * 3600

    @classmethod
    def from_settings(
        cls,
        *,
        cookies_path: Optional[str] = None,
        fallback_path: Optional[str] = None,
        browser_cookies_path: Optional[str] = None,
        legacy_env_vars: tuple[str, ...] = (
            "MERCADOLIBRE_COOKIES_PATH",
            "BOT_DIVERSIDAD_GLOBAL_COOKIES_PATH",
            "OFERTAS_MELI_BROWSER_COOKIES_PATH",
        ),
        save_cookies_on_exit: bool = True,
    ) -> "MercadoLibreSession":
        """Resuelve la mejor ruta disponible.

        Prioridad: `cookies_path` (si existe) → env vars → `fallback_path`.
        Si nada existe, devuelve el `cookies_path` para que `load()` reporte
        `is_missing=True`.
        """
        from pathlib import Path as _Path

        primary = cookies_path
        # Si el primary no existe pero hay fallback que sí, usamos fallback.
        if primary:
            primary_path = _Path(primary)
            if not primary_path.is_absolute():
                primary_path = _Path.cwd() / primary_path
            if not primary_path.exists() and fallback_path:
                fb = _Path(fallback_path)
                if not fb.is_absolute():
                    fb = _Path.cwd() / fb
                if fb.exists():
                    primary = str(fb)
                    logger.info(
                        "ML cookies: usando fallback %s (primary no existía)", fb
                    )

        resolved = resolve_cookie_path(
            primary=primary,
            env_vars=legacy_env_vars,
        )
        return cls(
            cookies_path=resolved,
            browser_cookies_path=Path(browser_cookies_path) if browser_cookies_path else None,
            save_cookies_on_exit=save_cookies_on_exit,
        )

    def store(self) -> CookieStore:
        return CookieStore(path=self.cookies_path, expiry_warn_seconds=self.expiry_warn_seconds)

    def load(self) -> tuple[list[dict], CookieHealth]:
        return self.store().load()

    def save(self, cookies: list[dict]) -> Path:
        return self.store().save(cookies)


def is_login_redirect(final_url: Optional[str]) -> bool:
    if not final_url:
        return False
    lowered = final_url.lower()
    return any(token in lowered for token in _LOGIN_REDIRECT_TOKENS)
