"""Tests del prefiltro de descuento integrado en DiscoveryAgent (ML)."""

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

    async def fetch(self, url: str) -> RenderedPage:
        return self.pages.get(
            url,
            RenderedPage(url=url, final_url=url, status=0, html="", error="not_mocked"),
        )

    async def aclose(self) -> None:  # pragma: no cover
        pass


def _ok(html: str, url: str) -> RenderedPage:
    return RenderedPage(url=url, final_url=url, status=200, html=html)


def _card(href: str, *, discount_text: str = "") -> str:
    disc = (
        f'<span class="andes-money-amount__discount">{discount_text}</span>'
        if discount_text
        else ""
    )
    return f"""
    <li class="ui-search-layout__item">
      <div class="ui-search-result__wrapper">
        <a class="ui-search-link" href="{href}">prod</a>
        {disc}
      </div>
    </li>
    """


def _ml_listing_mixed() -> str:
    """3 productos: 60% OFF, 50% OFF, 30% OFF."""
    return f"""
    <html><body><ul>
      {_card('/p/MLM00000060', discount_text='60% OFF')}
      {_card('/p/MLM00000050', discount_text='50% OFF')}
      {_card('/p/MLM00000030', discount_text='30% OFF')}
    </ul></body></html>
    """


@pytest.mark.asyncio
async def test_prefilter_only_admits_products_above_threshold(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    seed = "https://www.mercadolibre.com.mx/c/electronica"
    browser = FakeBrowserWorker({seed: _ok(_ml_listing_mixed(), seed)})
    agent = DiscoveryAgent(
        browser=browser,
        db_conn=conn,
        marketplace="mercadolibre",
        max_per_cycle=5,
        listing_discount_prefilter=True,
        listing_min_discount=50.0,
        listing_discount_strict=False,
    )
    agent.seed_from_config([seed])
    await agent.discover_once()

    f = FrontierRepo(conn)
    products = f.pop("mercadolibre", kind="product", limit=10)
    urls = {p.url for p in products}
    # 60% y 50% entran; 30% NO.
    assert "https://www.mercadolibre.com.mx/p/MLM00000060" in urls
    assert "https://www.mercadolibre.com.mx/p/MLM00000050" in urls
    assert "https://www.mercadolibre.com.mx/p/MLM00000030" not in urls
    conn.close()


@pytest.mark.asyncio
async def test_prefilter_disabled_admits_all_products(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    seed = "https://www.mercadolibre.com.mx/c/electronica"
    browser = FakeBrowserWorker({seed: _ok(_ml_listing_mixed(), seed)})
    agent = DiscoveryAgent(
        browser=browser,
        db_conn=conn,
        marketplace="mercadolibre",
        max_per_cycle=5,
        listing_discount_prefilter=False,
    )
    agent.seed_from_config([seed])
    await agent.discover_once()

    f = FrontierRepo(conn)
    products = f.pop("mercadolibre", kind="product", limit=10)
    # Sin prefiltro, los 3 entran (comportamiento legacy).
    assert len(products) == 3
    conn.close()


@pytest.mark.asyncio
async def test_prefilter_strict_drops_unknown_discount(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    # Producto sin texto de descuento → unknown.
    html = f"<html><body><ul>{_card('/p/MLM00000099')}</ul></body></html>"
    seed = "https://www.mercadolibre.com.mx/c/electronica"
    browser = FakeBrowserWorker({seed: _ok(html, seed)})
    agent = DiscoveryAgent(
        browser=browser,
        db_conn=conn,
        marketplace="mercadolibre",
        max_per_cycle=5,
        listing_discount_prefilter=True,
        listing_min_discount=50.0,
        listing_discount_strict=True,
    )
    agent.seed_from_config([seed])
    await agent.discover_once()

    f = FrontierRepo(conn)
    products = f.pop("mercadolibre", kind="product", limit=10)
    assert len(products) == 0
    conn.close()


@pytest.mark.asyncio
async def test_prefilter_lax_keeps_unknown_with_low_score(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    html = f"<html><body><ul>{_card('/p/MLM00000099')}</ul></body></html>"
    seed = "https://www.mercadolibre.com.mx/c/electronica"
    browser = FakeBrowserWorker({seed: _ok(html, seed)})
    agent = DiscoveryAgent(
        browser=browser,
        db_conn=conn,
        marketplace="mercadolibre",
        max_per_cycle=5,
        listing_discount_prefilter=True,
        listing_min_discount=50.0,
        listing_discount_strict=False,
        listing_unknown_discount_score=1.0,
    )
    agent.seed_from_config([seed])
    await agent.discover_once()

    f = FrontierRepo(conn)
    products = f.pop("mercadolibre", kind="product", limit=10)
    assert len(products) == 1
    assert products[0].score == 1.0
    conn.close()


@pytest.mark.asyncio
async def test_amazon_discovery_unaffected_by_ml_prefilter(tmp_path: Path):
    """El prefiltro es ML-only: Amazon mantiene su flujo legacy."""
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    amazon_html = """
    <html><body>
    <div data-component-type="s-search-result" data-asin="B0FRESH001"></div>
    <div data-component-type="s-search-result" data-asin="B0FRESH002"></div>
    </body></html>
    """
    seed = "https://www.amazon.com.mx/s?k=laptop"
    browser = FakeBrowserWorker({seed: _ok(amazon_html, seed)})
    agent = DiscoveryAgent(
        browser=browser,
        db_conn=conn,
        marketplace="amazon",
        max_per_cycle=5,
        listing_discount_prefilter=True,
        listing_min_discount=50.0,
        listing_discount_strict=True,  # estricto, pero NO debe afectar a Amazon
    )
    agent.seed_from_config([seed])
    await agent.discover_once()

    f = FrontierRepo(conn)
    products = f.pop("amazon", kind="product", limit=10)
    assert len(products) == 2
    conn.close()
