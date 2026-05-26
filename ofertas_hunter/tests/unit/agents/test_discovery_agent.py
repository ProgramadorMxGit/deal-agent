"""Tests del DiscoveryAgent con FakeBrowserWorker."""

from __future__ import annotations

from pathlib import Path

import pytest

from ofertas_hunter.agents.discovery_agent import DiscoveryAgent
from ofertas_hunter.browser.browser_context import RenderedPage
from ofertas_hunter.db import connect, init_db
from ofertas_hunter.exploration.frontier import FrontierRepo


class FakeBrowserWorker:
    def __init__(self, pages: dict[str, RenderedPage]) -> None:
        self.pages = pages
        self.fetched: list[str] = []

    async def fetch(self, url: str) -> RenderedPage:
        self.fetched.append(url)
        return self.pages.get(
            url,
            RenderedPage(url=url, final_url=url, status=0, html="", error="not_mocked"),
        )

    async def aclose(self) -> None:  # pragma: no cover
        pass


def _ok(html: str, url: str) -> RenderedPage:
    return RenderedPage(url=url, final_url=url, status=200, html=html)


def _amazon_search_html() -> str:
    return """
<html><body>
<div data-component-type="s-search-result" data-asin="B0FRESH001"></div>
<div data-component-type="s-search-result" data-asin="B0FRESH002"></div>
<div data-component-type="s-search-result" data-asin="B0FRESH003"></div>
<a class="s-pagination-next" href="/s?k=laptop&page=2">next</a>
</body></html>
"""


def _ml_listing_html() -> str:
    return """
<html><body>
<a class="ui-search-link" href="/p/MLM00000001">p1</a>
<a class="ui-search-link" href="/p/MLM00000002">p2</a>
<a class="ui-search-item__group__element" href="https://articulo.mercadolibre.com.mx/MLM00000003-foo">p3</a>
<a class="andes-pagination__link" href="/c/electronica?page=2">next</a>
</body></html>
"""


@pytest.mark.asyncio
async def test_discovery_seeds_categories_to_frontier(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    browser = FakeBrowserWorker({})

    agent = DiscoveryAgent(browser=browser, db_conn=conn, marketplace="amazon")
    n = agent.seed_from_config(
        [
            "https://www.amazon.com.mx/deals",
            "https://www.amazon.com.mx/s?k=laptop",
            "https://www.mercadolibre.com.mx/ofertas",  # marketplace distinto: ignorado
        ]
    )
    assert n == 2
    f = FrontierRepo(conn)
    assert f.count_pending("amazon") == 2
    assert f.count_pending("mercadolibre") == 0
    conn.close()


@pytest.mark.asyncio
async def test_discovery_extracts_products_to_frontier(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    seed_url = "https://www.amazon.com.mx/s?k=laptop"
    browser = FakeBrowserWorker({seed_url: _ok(_amazon_search_html(), seed_url)})
    agent = DiscoveryAgent(browser=browser, db_conn=conn, marketplace="amazon", max_per_cycle=5)
    agent.seed_from_config([seed_url])

    outcomes = await agent.discover_once()
    assert len(outcomes) == 1
    assert outcomes[0].discovered_count >= 3  # 3 productos + 1 paginación
    assert outcomes[0].persisted_count >= 3

    f = FrontierRepo(conn)
    products = f.pop("amazon", kind="product", limit=10)
    assert len(products) == 3
    assert {p.url for p in products} == {
        "https://www.amazon.com.mx/dp/B0FRESH001",
        "https://www.amazon.com.mx/dp/B0FRESH002",
        "https://www.amazon.com.mx/dp/B0FRESH003",
    }

    # La URL de listing fue marcada como visitada.
    assert f.is_visited("amazon", seed_url)
    conn.close()


@pytest.mark.asyncio
async def test_discovery_paginates_into_frontier(tmp_path: Path):
    """La paginación detectada se mete al frontier para próximos ciclos."""
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    seed_url = "https://www.amazon.com.mx/s?k=laptop"
    browser = FakeBrowserWorker({seed_url: _ok(_amazon_search_html(), seed_url)})
    agent = DiscoveryAgent(browser=browser, db_conn=conn, marketplace="amazon", max_per_cycle=5)
    agent.seed_from_config([seed_url])
    await agent.discover_once()

    f = FrontierRepo(conn)
    listings = f.pop("amazon", kind="listing", limit=10)
    # La paginación `?page=2` queda en el frontier.
    assert any("page=2" in i.url for i in listings)
    conn.close()


@pytest.mark.asyncio
async def test_discovery_handles_login_redirect(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    seed = "https://www.mercadolibre.com.mx/c/electronica"
    final = "https://www.mercadolibre.com.mx/gz/account-verification"
    browser = FakeBrowserWorker(
        {seed: RenderedPage(url=seed, final_url=final, status=200, html="<html>login</html>")}
    )
    agent = DiscoveryAgent(browser=browser, db_conn=conn, marketplace="mercadolibre", max_per_cycle=1)
    agent.seed_from_config([seed])
    outcomes = await agent.discover_once()

    assert outcomes[0].discarded_reason == "login_redirect"
    rows = conn.execute("SELECT kind FROM runtime_events WHERE kind = 'cookie_expiry'").fetchall()
    assert len(rows) == 1
    snaps = conn.execute("SELECT reason FROM dom_snapshots").fetchall()
    assert any(s["reason"] == "login_redirect" for s in snaps)
    conn.close()


@pytest.mark.asyncio
async def test_discovery_handles_captcha(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    seed = "https://www.amazon.com.mx/s?k=laptop"
    browser = FakeBrowserWorker(
        {
            seed: RenderedPage(
                url=seed,
                final_url=seed,
                status=503,
                html="captcha",
                error="captcha_detected",
                blocked=True,
            )
        }
    )
    agent = DiscoveryAgent(browser=browser, db_conn=conn, marketplace="amazon", max_per_cycle=1)
    agent.seed_from_config([seed])
    outcomes = await agent.discover_once()

    assert outcomes[0].discarded_reason == "captcha_detected"
    snaps = conn.execute("SELECT reason FROM dom_snapshots").fetchall()
    assert any(s["reason"] == "fetch_failed" for s in snaps)
    conn.close()


@pytest.mark.asyncio
async def test_ml_discovery_extracts_products(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    seed = "https://www.mercadolibre.com.mx/c/electronica"
    browser = FakeBrowserWorker({seed: _ok(_ml_listing_html(), seed)})
    agent = DiscoveryAgent(browser=browser, db_conn=conn, marketplace="mercadolibre", max_per_cycle=5)
    agent.seed_from_config([seed])
    outcomes = await agent.discover_once()

    assert outcomes[0].discovered_count >= 3
    f = FrontierRepo(conn)
    products = f.pop("mercadolibre", kind="product", limit=10)
    assert len(products) == 3
    conn.close()
