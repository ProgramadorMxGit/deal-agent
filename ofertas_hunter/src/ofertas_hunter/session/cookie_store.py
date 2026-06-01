"""Cookie store genérico para Playwright.

Carga cookies en JSON (formato Playwright o Cookie-Editor/EditThisCookie),
las normaliza al esquema que Playwright acepta y opcionalmente las guarda
de vuelta al cerrar el contexto.

Reutiliza la lógica robusta del legacy `bot_diversidad_global/src/session_loader.py`,
extendida con:

- Estado de salud (`CookieHealth`) para detectar expiración cercana o vacío.
- API `save(cookies)` que vuelve a persistir las cookies obtenidas del context.
- Soporte para múltiples paths fallback (env vars + default), conservando
  compatibilidad con el legacy (`BOT_DIVERSIDAD_GLOBAL_COOKIES_PATH`,
  `OFERTAS_MELI_BROWSER_COOKIES_PATH`).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


logger = logging.getLogger(__name__)


_VALID_SAMESITE = {"Strict", "Lax", "None"}
_SAMESITE_MAP = {
    "no_restriction": "None",
    "lax": "Lax",
    "strict": "Strict",
    "none": "None",
}
_PLAYWRIGHT_FIELDS = {
    "name",
    "value",
    "domain",
    "path",
    "expires",
    "httpOnly",
    "secure",
    "sameSite",
}


@dataclass
class CookieHealth:
    """Diagnóstico del archivo de cookies.

    `loaded`: cuántas cookies válidas se cargaron.
    `total_in_file`: cuántas estaban en el JSON original.
    `path`: archivo desde el que se cargaron (None si no existe).
    `is_empty`: True si no hay cookies útiles.
    `is_missing`: True si el archivo no existe o no es JSON válido.
    `expiring_soon`: cookies con `expires` < `now + warn_seconds`.
    """

    path: Optional[Path]
    loaded: int = 0
    total_in_file: int = 0
    is_missing: bool = False
    is_empty: bool = True
    expiring_soon: int = 0
    error: Optional[str] = None

    @property
    def healthy(self) -> bool:
        return not self.is_missing and not self.is_empty


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def resolve_cookie_path(
    *,
    primary: Optional[str | Path] = None,
    env_vars: Iterable[str] = (
        "MERCADOLIBRE_COOKIES_PATH",
        "BOT_DIVERSIDAD_GLOBAL_COOKIES_PATH",
        "OFERTAS_MELI_BROWSER_COOKIES_PATH",
    ),
    default: str = "secrets/mercadolibre_cookies.json",
) -> Path:
    """Devuelve el primer path existente entre `primary`, env vars y `default`.

    No falla si nada existe; devuelve el `primary` (o default) para que el
    `load()` lo reporte como `is_missing=True` y el agente decida pausar.
    """
    candidates: list[Path] = []
    if primary:
        candidates.append(_normalize(primary))
    for env in env_vars:
        raw = os.environ.get(env, "").strip()
        if raw:
            candidates.append(_normalize(raw))
    candidates.append(_normalize(default))

    for path in candidates:
        if path.exists():
            return path
    # Ninguno existe; devolvemos el primero (primary o default).
    return candidates[0]


def _normalize(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


# ---------------------------------------------------------------------------
# Sanitización
# ---------------------------------------------------------------------------


def sanitize_cookie(cookie: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Devuelve un dict apto para Playwright `add_cookies`, o None."""
    if not isinstance(cookie, dict):
        return None
    c = dict(cookie)

    if not c.get("name") or "value" not in c or c.get("value") is None:
        return None

    if "expirationDate" in c and "expires" not in c:
        c["expires"] = c.pop("expirationDate")
    if "expiry" in c and "expires" not in c:
        c["expires"] = c.pop("expiry")

    # session=True con expires definido → Playwright rechaza; quitamos expires.
    if c.get("session") is True and "expires" in c:
        del c["expires"]

    if "sameSite" in c:
        raw = c["sameSite"]
        if raw is None:
            del c["sameSite"]
        elif raw in _VALID_SAMESITE:
            pass
        elif isinstance(raw, str) and raw.lower() in _SAMESITE_MAP:
            c["sameSite"] = _SAMESITE_MAP[raw.lower()]
        else:
            del c["sameSite"]

    for unknown in list(set(c.keys()) - _PLAYWRIGHT_FIELDS):
        del c[unknown]

    # Coerciones suaves
    if "expires" in c and c["expires"] is not None:
        try:
            c["expires"] = float(c["expires"])
        except (TypeError, ValueError):
            del c["expires"]

    return c


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@dataclass
class CookieStore:
    """Carga / guarda / diagnostica cookies de un marketplace."""

    path: Path
    expiry_warn_seconds: int = 7 * 24 * 3600

    @classmethod
    def for_path(cls, path: str | Path) -> "CookieStore":
        return cls(path=_normalize(path))

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load(self) -> tuple[list[dict[str, Any]], CookieHealth]:
        if not self.path.exists():
            return [], CookieHealth(path=self.path, is_missing=True, is_empty=True)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            return [], CookieHealth(
                path=self.path, is_missing=True, is_empty=True, error=str(exc)
            )

        if not isinstance(raw, list):
            return [], CookieHealth(
                path=self.path,
                is_missing=False,
                is_empty=True,
                error=f"expected JSON array, got {type(raw).__name__}",
            )

        sanitized: list[dict[str, Any]] = []
        for entry in raw:
            cookie = sanitize_cookie(entry)
            if cookie is not None:
                sanitized.append(cookie)

        now = time.time()
        warn_cutoff = now + self.expiry_warn_seconds
        expiring = sum(
            1 for c in sanitized if c.get("expires") and c["expires"] <= warn_cutoff
        )

        return sanitized, CookieHealth(
            path=self.path,
            loaded=len(sanitized),
            total_in_file=len(raw),
            is_missing=False,
            is_empty=len(sanitized) == 0,
            expiring_soon=expiring,
        )

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self, cookies: list[dict[str, Any]]) -> Path:
        """Persiste las cookies (lista de dicts) en el path configurado.

        Hace dump atómico (file.tmp → rename) para evitar corrupción.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Sanitizamos antes de guardar para evitar strings raros.
        cleaned = [sanitize_cookie(c) or c for c in cookies]
        cleaned = [c for c in cleaned if c is not None]
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        return self.path
