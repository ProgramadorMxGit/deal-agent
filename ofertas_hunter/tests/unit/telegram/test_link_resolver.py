"""Tests del LinkResolver."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from ofertas_hunter.db import init_db, connect
from ofertas_hunter.telegram.link_resolver import (
    LinkResolver,
    is_mercadolibre_url,
    is_shortlink,
)


def _mk_handler(redirects: dict[str, str], final_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in redirects:
            return httpx.Response(302, headers={"Location": redirects[url]})
        return httpx.Response(final_status)

    return handler


@pytest.mark.asyncio
async def test_telegram_resolves_shortlinks_with_mock_transport():
    handler = _mk_handler(
        {
            "https://bit.ly/abc": "https://www.walmart.com.mx/p/example",
        }
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as session:
        resolver = LinkResolver(client=session)
        resolved = await resolver.resolve("https://bit.ly/abc")

    assert resolved.resolved is True
    assert resolved.final_url == "https://www.walmart.com.mx/p/example"
    assert resolved.is_mercadolibre is False


@pytest.mark.asyncio
async def test_telegram_handles_link_resolver_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as session:
        resolver = LinkResolver(client=session, timeout_seconds=0.5)
        resolved = await resolver.resolve("https://bit.ly/timeout")

    assert resolved.resolved is False
    assert resolved.final_url is None
    assert "http_error" in (resolved.error or "")
    # Aún debe preservar la original_url para que el listener decida.
    assert resolved.best_url() == "https://bit.ly/timeout"


@pytest.mark.asyncio
async def test_resolver_detects_mercadolibre_after_redirect():
    handler = _mk_handler(
        {"https://bit.ly/meli": "https://www.mercadolibre.com.mx/p/MLM12345"}
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as session:
        resolver = LinkResolver(client=session)
        resolved = await resolver.resolve("https://bit.ly/meli")

    assert resolved.resolved is True
    assert resolved.is_mercadolibre is True


@pytest.mark.asyncio
async def test_resolver_skips_redirects_for_direct_url():
    handler = _mk_handler({})
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as session:
        resolver = LinkResolver(client=session)
        resolved = await resolver.resolve("https://www.walmart.com.mx/p/x")

    assert resolved.resolved is True
    assert resolved.final_url == "https://www.walmart.com.mx/p/x"
    assert resolved.from_cache is False


@pytest.mark.asyncio
async def test_resolver_uses_sqlite_cache(tmp_path: Path):
    db_path = tmp_path / "cache.db"
    init_db(db_path)
    conn = connect(db_path)

    handler = _mk_handler({"https://bit.ly/abc": "https://amzn.to/xyz"})
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as session:
        resolver = LinkResolver(client=session, cache_conn=conn)
        first = await resolver.resolve("https://bit.ly/abc")
        assert first.resolved is True
        assert first.from_cache is False

        second = await resolver.resolve("https://bit.ly/abc")
        assert second.from_cache is True
        assert second.final_url == first.final_url

    conn.close()


def test_helpers():
    assert is_shortlink("https://bit.ly/x") is True
    assert is_shortlink("https://amzn.to/x") is True
    assert is_shortlink("https://walmart.com.mx/p/x") is False
    assert is_mercadolibre_url("https://meli.la/abc") is True
    assert is_mercadolibre_url("https://www.mercadolibre.com.mx/p/MLM12345") is True
    assert is_mercadolibre_url("https://amazon.com.mx") is False
