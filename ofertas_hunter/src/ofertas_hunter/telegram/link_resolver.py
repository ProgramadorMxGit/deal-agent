"""Resolver de shortlinks para mensajes de Telegram.

Resuelve `bit.ly/...`, `tinyurl.com/...`, `t.co/...`, `goo.gl/...`,
`amzn.to/...`, `meli.la/...` siguiendo redirects con HEAD/GET. Devuelve la URL
final, el `http_status` y un flag `resolved`.

Si el resolver falla (timeout, DNS, 5xx, demasiados redirects), preserva la
URL original con `resolved=False` y registra el motivo. **Nunca lanza** —
es responsabilidad del listener decidir qué hacer.

Cache opcional vía `sqlite3.Connection`: tabla `resolved_urls`
(`original_url, final_url, http_status, resolved_at`). Si está disponible,
no se vuelve a hacer red por la misma URL.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx


logger = logging.getLogger(__name__)


_SHORTLINK_HOSTS: tuple[str, ...] = (
    "bit.ly",
    "tinyurl.com",
    "t.co",
    "goo.gl",
    "amzn.to",
    "amzn.mx",
    "meli.la",
    "rebrand.ly",
    "ow.ly",
    "buff.ly",
    "lnk.bio",
    "shorturl.at",
)

_MERCADOLIBRE_HOSTS: tuple[str, ...] = (
    "mercadolibre.com.mx",
    "www.mercadolibre.com.mx",
    "mercadolibre.com",
    "articulo.mercadolibre.com.mx",
    "listado.mercadolibre.com.mx",
    "click1.mercadolibre.com.mx",
    "meli.la",
)


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedLink:
    original_url: str
    final_url: Optional[str]
    http_status: Optional[int]
    resolved: bool
    is_mercadolibre: bool
    error: Optional[str] = None
    from_cache: bool = False

    def best_url(self) -> str:
        """Mejor URL disponible (final si resuelve, original si no)."""
        return self.final_url or self.original_url


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_mercadolibre_url(url: Optional[str]) -> bool:
    if not url:
        return False
    return _host_matches(url, _MERCADOLIBRE_HOSTS)


def is_shortlink(url: Optional[str]) -> bool:
    if not url:
        return False
    return _host_matches(url, _SHORTLINK_HOSTS)


def _host_matches(url: str, hosts: tuple[str, ...]) -> bool:
    """Compara host exacto (con o sin puerto), no substring."""
    try:
        parsed = httpx.URL(url)
    except Exception:
        return False
    host = (parsed.host or "").lower()
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in hosts)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class LinkResolver:
    """Resolver async de shortlinks con cache SQLite opcional."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 6.0,
        max_redirects: int = 5,
        cache_conn: Optional[sqlite3.Connection] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_redirects = max_redirects
        self.cache_conn = cache_conn
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "LinkResolver":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def resolve(self, url: str) -> ResolvedLink:
        """Resuelve una URL siguiendo redirects.

        - Si la URL ya es directa de un retailer, devuelve `ResolvedLink`
          sin tocar la red (`resolved=True`, `from_cache=False`,
          `final_url=url`).
        - Si la URL está en cache, la devuelve.
        - Si falla la red, preserva `original_url` y `resolved=False`.
        """
        if not url:
            return ResolvedLink(
                original_url=url, final_url=None, http_status=None,
                resolved=False, is_mercadolibre=False, error="empty_url",
            )

        # Cache hit
        cached = self._lookup_cache(url)
        if cached is not None:
            return cached

        # Si no es shortlink, no hace falta resolver: ya es la URL final.
        if not is_shortlink(url):
            result = ResolvedLink(
                original_url=url,
                final_url=url,
                http_status=None,
                resolved=True,
                is_mercadolibre=is_mercadolibre_url(url),
            )
            self._save_cache(result)
            return result

        # Resolver siguiendo redirects manualmente.
        result = await self._follow_redirects(url)
        self._save_cache(result)
        return result

    # ------------------------------------------------------------------
    # Redirects manuales
    # ------------------------------------------------------------------

    async def _follow_redirects(self, url: str) -> ResolvedLink:
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=False, timeout=self.timeout_seconds
            )

        current = url
        last_status: Optional[int] = None
        for hop in range(self.max_redirects):
            try:
                resp = await self._client.head(
                    current,
                    headers={"User-Agent": _user_agent()},
                    timeout=self.timeout_seconds,
                )
            except httpx.HTTPError as exc:
                # Algunos hosts no aceptan HEAD; reintenta GET una sola vez.
                if hop == 0:
                    try:
                        resp = await self._client.get(
                            current,
                            headers={"User-Agent": _user_agent()},
                            timeout=self.timeout_seconds,
                        )
                    except httpx.HTTPError as exc2:
                        return ResolvedLink(
                            original_url=url,
                            final_url=None,
                            http_status=None,
                            resolved=False,
                            is_mercadolibre=False,
                            error=f"http_error: {exc2}",
                        )
                else:
                    return ResolvedLink(
                        original_url=url,
                        final_url=None,
                        http_status=None,
                        resolved=False,
                        is_mercadolibre=False,
                        error=f"http_error: {exc}",
                    )

            last_status = resp.status_code

            if 300 <= resp.status_code < 400 and "location" in resp.headers:
                next_url = resp.headers["location"]
                # Algunos shortlinks devuelven path relativo
                if next_url.startswith("/"):
                    base = httpx.URL(current)
                    next_url = str(base.copy_with(path=next_url))
                current = next_url
                continue

            # Status final
            return ResolvedLink(
                original_url=url,
                final_url=current,
                http_status=resp.status_code,
                resolved=True,
                is_mercadolibre=is_mercadolibre_url(current),
            )

        return ResolvedLink(
            original_url=url,
            final_url=current,
            http_status=last_status,
            resolved=False,
            is_mercadolibre=is_mercadolibre_url(current),
            error="max_redirects_exceeded",
        )

    # ------------------------------------------------------------------
    # Cache SQLite
    # ------------------------------------------------------------------

    def _lookup_cache(self, url: str) -> Optional[ResolvedLink]:
        if self.cache_conn is None:
            return None
        try:
            row = self.cache_conn.execute(
                "SELECT final_url, http_status FROM resolved_urls WHERE original_url=?",
                (url,),
            ).fetchone()
        except sqlite3.Error as exc:
            logger.debug("link_resolver cache read failed: %s", exc)
            return None
        if row is None:
            return None
        final_url = row["final_url"] if isinstance(row, sqlite3.Row) else row[0]
        http_status = row["http_status"] if isinstance(row, sqlite3.Row) else row[1]
        return ResolvedLink(
            original_url=url,
            final_url=final_url,
            http_status=http_status,
            resolved=final_url is not None,
            is_mercadolibre=is_mercadolibre_url(final_url),
            from_cache=True,
        )

    def _save_cache(self, result: ResolvedLink) -> None:
        if self.cache_conn is None:
            return
        try:
            self.cache_conn.execute(
                "INSERT OR REPLACE INTO resolved_urls "
                "(original_url, final_url, http_status, resolved_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    result.original_url,
                    result.final_url,
                    result.http_status,
                    datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
                        "+00:00", "Z"
                    ),
                ),
            )
        except sqlite3.Error as exc:
            logger.debug("link_resolver cache write failed: %s", exc)


def _user_agent() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
