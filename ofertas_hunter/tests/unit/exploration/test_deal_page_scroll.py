"""Tests de scroll/lazy-loading en deal pages + métricas de conversión.

Cubre:
- DiscoveryAgent pide scroll SOLO en listing/category/deals (no en product).
- El worker scrollea hasta max_products y se detiene temprano si no hay más.
- El scroll no duplica product URLs (dedupe del extractor se mantiene).
- emit `deal_page_scroll_applied` cuando aplica scroll.
- conversion_metrics.compute_conversion agrega el embudo sin tocar nada.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ofertas_hunter.agents.discovery_agent import DiscoveryAgent
from ofertas_hunter.browser.browser_context import BrowserConfig, RenderedPage
from ofertas_hunter.db import connect, init_db
from ofertas_hunter.exploration.frontier import FrontierRepo
from ofertas_hunter.exploration.conversion_metrics import (
    ConversionConfig,
    compute_conversion,
    emit_conversion_summary,
)


# ---------------------------------------------------------------------------
# Fake worker que registra si se pidió scroll y soporta el kwarg nuevo.
# ---------------------------------------------------------------------------
class ScrollAwareBrowser:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []  # (url, scroll_flag)

    async def fetch(self, url, *, scroll_for_lazy_load=False):
        self.calls.append((url, scroll_for_lazy_load))
        return self.pages.get(
            url, RenderedPage(url=url, final_url=url, status=0, html="", error="nf")
        )

    async def aclose(self):
        pass


@pytest.fixture
def db_path(tmp_path: Path):
    init_db(tmp_path / "d.db")
    return tmp_path / "d.db"


def _ok(html, url):
    return RenderedPage(url=url, final_url=url, status=200, html=html)


_AMZ_DEALS = """
<html><body>
<div data-asin="B0DEAL0001"></div>
<div data-asin="B0DEAL0002"></div>
<a href="/dp/B0DEAL0003">x</a>
<a href="/dp/B0DEAL0001">dup</a>
</body></html>
"""


def test_scroll_requested_only_for_deal_pages(db_path):
    conn = connect(db_path)
    frontier_url = "https://www.amazon.com.mx/deals?bubble-id=1"
    fr = FrontierRepo(conn)
    fr.add(frontier_url, kind="deals", score=20.0)

    browser = ScrollAwareBrowser({frontier_url: _ok(_AMZ_DEALS, frontier_url)})
    agent = DiscoveryAgent(browser, db_conn=conn, marketplace="amazon", max_per_cycle=3)

    import asyncio
    outcomes = asyncio.run(agent.discover_once())
    conn.commit()

    # Se pidió scroll en la deal page.
    assert browser.calls[0] == (frontier_url, True)
    # Se emitió el evento de scroll.
    n = conn.execute(
        "SELECT COUNT(*) FROM runtime_events WHERE kind='deal_page_scroll_applied'"
    ).fetchone()[0]
    assert n == 1
    # Extrajo product URLs sin duplicar (B0DEAL0001 aparece 2 veces en HTML).
    assert outcomes[0].discovered_count >= 3
    prods = conn.execute(
        "SELECT COUNT(*) FROM frontier WHERE url_type='product'"
    ).fetchone()[0]
    # 3 ASINs únicos (0001, 0002, 0003), sin duplicar 0001.
    assert prods == 3
    conn.close()


def test_product_page_not_scrolled(db_path):
    # Un kind=product nunca debe pasar por discover (sólo listing/category/deals).
    conn = connect(db_path)
    fr = FrontierRepo(conn)
    fr.add("https://www.amazon.com.mx/dp/B0PRODUCT01", kind="product", score=5.0)
    browser = ScrollAwareBrowser({})
    agent = DiscoveryAgent(browser, db_conn=conn, marketplace="amazon", max_per_cycle=3)
    import asyncio
    outcomes = asyncio.run(agent.discover_once())
    # No hay targets de tipo listing/deals → no se fetchea nada.
    assert outcomes == []
    assert browser.calls == []
    conn.close()


# ---------------------------------------------------------------------------
# Scroll del worker: respeta max_products y para temprano (mock de page).
# ---------------------------------------------------------------------------
class FakePage:
    """Simula una page Playwright para _scroll_for_lazy_load."""

    def __init__(self, counts):
        # counts: lista de valores que devuelve evaluate(count_js) por paso.
        self._counts = list(counts)
        self._idx = -1
        self.scrolls = 0      # scrolls progresivos (no cuenta el reset final)
        self.reset_scrolls = 0

    async def evaluate(self, script, *args):
        if "scrollTo" in script:
            # El reset final es scrollTo(0, 0) sin args de fracción.
            if not args:
                self.reset_scrolls += 1
            else:
                self.scrolls += 1
            return None
        if "scrollHeight" in script and "querySelectorAll" not in script:
            return 5000
        # count_js
        self._idx += 1
        if self._idx < len(self._counts):
            return self._counts[self._idx]
        return self._counts[-1] if self._counts else 0


def test_worker_scroll_stops_at_max_products():
    from ofertas_hunter.browser.playwright_worker import PlaywrightBrowserWorker
    cfg = BrowserConfig(
        deal_page_scroll_steps=6,
        deal_page_scroll_wait_ms=0,
        deal_page_max_products=10,
        deal_page_timeout_seconds=30,
    )
    w = PlaywrightBrowserWorker(cfg)
    # cards: 4, 8, 12 → al llegar a 12 (>=10) debe parar (3 scrolls).
    page = FakePage([4, 8, 12, 14, 16, 18])
    import asyncio
    asyncio.run(w._scroll_for_lazy_load(page))
    assert page.scrolls == 3


def test_worker_scroll_stops_when_stagnant():
    from ofertas_hunter.browser.playwright_worker import PlaywrightBrowserWorker
    cfg = BrowserConfig(
        deal_page_scroll_steps=6,
        deal_page_scroll_wait_ms=0,
        deal_page_max_products=100,
        deal_page_timeout_seconds=30,
    )
    w = PlaywrightBrowserWorker(cfg)
    # cards no crecen: 5,5,5 → para tras 2 pasos estancados (3 scrolls).
    page = FakePage([5, 5, 5, 5, 5, 5])
    import asyncio
    asyncio.run(w._scroll_for_lazy_load(page))
    assert page.scrolls == 3  # 1er paso + 2 estancados


# ---------------------------------------------------------------------------
# Conversion metrics: agrega embudo, no toca nada, emite evento.
# ---------------------------------------------------------------------------
def test_conversion_metrics_aggregates(db_path):
    conn = connect(db_path)
    now = "2026-05-31T15:00:00.000Z"
    # deal page scroll event
    conn.execute(
        "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
        "VALUES ('deal_page_scroll_applied','info',?,?)",
        (json.dumps({"marketplace": "amazon"}), now),
    )
    # product + visited
    conn.execute(
        "INSERT INTO products (marketplace, marketplace_id, url_canonical, title, "
        "first_seen_at, last_seen_at) VALUES ('amazon','A1','https://a/dp/A1','t',?,?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO visited_urls (marketplace, url_canonical, visited_at) "
        "VALUES ('amazon','https://a/dp/A1',?)",
        (now,),
    )
    conn.commit()

    since = "2026-05-31T14:00:00.000Z"
    summary = compute_conversion(conn, marketplace="amazon", since_iso=since)
    assert summary["marketplace"] == "amazon"
    assert summary["deal_pages_visited"] == 1
    assert summary["product_urls_extracted"] == 1
    assert summary["pdp_visited"] == 1
    # emit no lanza y escribe 1 evento por marketplace
    out = emit_conversion_summary(conn, window_minutes=60, marketplaces=("amazon",))
    assert len(out) == 1
    n = conn.execute(
        "SELECT COUNT(*) FROM runtime_events WHERE kind='discovery_conversion_summary'"
    ).fetchone()[0]
    assert n == 1
    conn.close()
