from __future__ import annotations

import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from ofertas_hunter.config import Settings
from ofertas_hunter.db import init_db
from ofertas_hunter.mcp.context import ServerContext


@pytest.fixture
def db(tmp_path: Path):
    path = tmp_path / "test.db"
    init_db(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.mark.asyncio
async def test_server_context_get_amazon_hunter_loads_cookies_and_injects_affiliate_extractor(
    db,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cookies_path = tmp_path / "amazon_cookies.json"
    cookies_path.write_text(
        json.dumps(
            [
                {
                    "name": "sid",
                    "value": "abc",
                    "domain": ".amazon.com.mx",
                    "path": "/",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class FakeBrowserConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeContext:
        def __init__(self) -> None:
            self.cookies = []

        async def add_cookies(self, cookies):
            self.cookies.extend(cookies)

    class FakeBrowserWorker:
        def __init__(self, config):
            self.config = config
            self._context = FakeContext()

        async def _ensure_started(self):
            return None

        async def aclose(self):
            return None

    monkeypatch.setitem(
        sys.modules,
        "ofertas_hunter.browser.browser_context",
        types.SimpleNamespace(
            BrowserConfig=FakeBrowserConfig,
            BrowserWorker=object,
            RenderedPage=object,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "ofertas_hunter.browser.playwright_worker",
        types.SimpleNamespace(
            PlaywrightBrowserWorker=FakeBrowserWorker,
            PlaywrightImportError=RuntimeError,
        ),
    )

    settings = Settings(
        amazon_enabled=True,
        amazon_hunter_legacy=False,
        amazon_cookies_path=str(cookies_path),
    )
    ctx = ServerContext.build(db=db, settings=settings)
    try:
        hunter = await ctx.get_amazon_hunter()
        assert hunter.affiliate_extractor is not None
        assert ctx._amazon_browser is not None
        assert ctx._amazon_browser._context.cookies[0]["name"] == "sid"
    finally:
        await ctx.aclose()
