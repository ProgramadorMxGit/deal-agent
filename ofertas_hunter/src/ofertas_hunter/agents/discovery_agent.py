"""DiscoveryAgent: descubre productos nuevos visitando listings/categorías.

Por marketplace:
- Lee seeds de categorías/búsqueda desde `config/seeds/<marketplace>.json`.
- Las añade al `frontier` con `kind=listing|category|deals`.
- En cada ciclo:
  1. `frontier.pop(kind in {listing,category,deals})`.
  2. `browser.fetch(url)`.
  3. `extract_*_listing(html)` → lista de URLs de productos + paginación.
  4. Las nuevas URLs se persisten en `frontier`.
  5. Marca la URL fetcheada como visitada.

Los hunters (Amazon, ML) consumen URLs `kind=product` del mismo frontier.

Defensas:
- Captcha / login wall → guarda snapshot, marca runtime_event, sigue con la
  siguiente URL.
- Sin Playwright → loop ocioso (no crashea).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..browser.browser_context import BrowserWorker, RenderedPage
from ..exploration.frontier import FrontierItem, FrontierRepo
from ..exploration.listing_extractor import (
    extract_amazon_deals,
    extract_amazon_listing,
    extract_mercadolibre_listing,
)
from ..exploration.url_classifier import classify
from ..session.mercadolibre_session import is_login_redirect


logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass
class DiscoveryOutcome:
    marketplace: str
    url: str
    final_url: str
    kind: str
    discovered_count: int
    persisted_count: int
    discarded_reason: Optional[str] = None
    snapshot_id: Optional[int] = None


class DiscoveryAgent:
    """Agente único que sirve a Amazon y ML según el marketplace de cada URL."""

    def __init__(
        self,
        browser: BrowserWorker,
        *,
        db_conn: sqlite3.Connection,
        marketplace: str,
        max_per_cycle: int = 3,
    ) -> None:
        self.browser = browser
        self.db = db_conn
        self.marketplace = marketplace
        self.max_per_cycle = max_per_cycle
        self.frontier = FrontierRepo(db_conn)

    async def aclose(self) -> None:
        try:
            await self.browser.aclose()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Seed loading
    # ------------------------------------------------------------------

    def seed_from_config(self, urls: list[str]) -> int:
        """Carga URLs de categorías/búsqueda al frontier (idempotente)."""
        n = 0
        for url in urls:
            info = classify(url)
            if info.marketplace != self.marketplace:
                continue
            if info.kind == "unknown":
                continue
            if self.frontier.add_classified(info):
                n += 1
        return n

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def discover_once(self) -> list[DiscoveryOutcome]:
        outcomes: list[DiscoveryOutcome] = []

        # Tomamos URLs de listing/category/deals del frontier.
        items = self.frontier.peek(self.marketplace, kind=None, limit=self.max_per_cycle * 4)
        targets = [it for it in items if it.kind in ("listing", "category", "deals")]
        targets = targets[: self.max_per_cycle]

        if not targets:
            return outcomes

        # Borrar del frontier sólo los targets (no productos).
        ids = tuple(it.id for it in targets)
        if ids:
            placeholders = ",".join("?" for _ in ids)
            self.db.execute(f"DELETE FROM frontier WHERE id IN ({placeholders})", ids)

        for item in targets:
            outcome = await self._discover_one(item)
            outcomes.append(outcome)
        return outcomes

    async def _discover_one(self, item: FrontierItem) -> DiscoveryOutcome:
        page = await self.browser.fetch(item.url)
        final_url = page.final_url or item.url

        if is_login_redirect(final_url):
            self._save_snapshot(item, page, "login_redirect")
            self._emit_runtime_event(
                "cookie_expiry",
                "warning",
                {"agent": "discovery", "marketplace": self.marketplace, "url": item.url},
            )
            return DiscoveryOutcome(
                marketplace=self.marketplace,
                url=item.url,
                final_url=final_url,
                kind=item.kind,
                discovered_count=0,
                persisted_count=0,
                discarded_reason="login_redirect",
            )

        if not page.ok:
            snap_id = self._save_snapshot(item, page, "fetch_failed")
            reason = "captcha_detected" if page.blocked else (page.error or "fetch_failed")
            return DiscoveryOutcome(
                marketplace=self.marketplace,
                url=item.url,
                final_url=final_url,
                kind=item.kind,
                discovered_count=0,
                persisted_count=0,
                discarded_reason=reason,
                snapshot_id=snap_id,
            )

        self.frontier.mark_visited(self.marketplace, item.url)

        discovered = self._extract_urls(item.kind, page.html, final_url)
        persisted = 0
        for info in discovered:
            if self.frontier.add_classified(info):
                persisted += 1

        logger.info(
            "discovery: %s [%s] -> found=%d persisted=%d",
            item.url,
            item.kind,
            len(discovered),
            persisted,
        )

        return DiscoveryOutcome(
            marketplace=self.marketplace,
            url=item.url,
            final_url=final_url,
            kind=item.kind,
            discovered_count=len(discovered),
            persisted_count=persisted,
        )

    def _extract_urls(self, kind: str, html: str, base_url: str) -> list:
        if self.marketplace == "amazon":
            if kind == "deals":
                return extract_amazon_deals(html, base_url=base_url)
            return extract_amazon_listing(html, base_url=base_url)
        if self.marketplace == "mercadolibre":
            return extract_mercadolibre_listing(html, base_url=base_url)
        return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _save_snapshot(
        self, item: FrontierItem, page: RenderedPage, reason: str
    ) -> Optional[int]:
        cur = self.db.execute(
            "INSERT INTO dom_snapshots (marketplace, context, url, content, captured_at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.marketplace,
                f"discovery_{item.kind}",
                item.url,
                page.html or "",
                _now_iso(),
                reason,
            ),
        )
        return cur.lastrowid

    def _emit_runtime_event(self, kind: str, severity: str, payload: dict) -> None:
        self.db.execute(
            "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (kind, severity, json.dumps(payload, ensure_ascii=False), _now_iso()),
        )
