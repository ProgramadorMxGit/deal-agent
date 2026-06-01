"""Tests de la sesión Amazon (cookies + bootstrap del browser)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ofertas_hunter.session.amazon_session import AmazonSession


def test_amazon_session_from_settings_uses_primary_cookie_path(tmp_path: Path):
    cookies_path = tmp_path / "amazon_cookies.json"
    cookies_path.write_text("[]", encoding="utf-8")

    session = AmazonSession.from_settings(cookies_path=str(cookies_path))

    assert session.cookies_path == cookies_path


@pytest.mark.asyncio
async def test_amazon_session_injects_cookies_into_browser_context(tmp_path: Path):
    cookies_path = tmp_path / "amazon_cookies.json"
    cookies_path.write_text(
        json.dumps(
            [
                {
                    "name": "sid",
                    "value": "abc",
                    "domain": ".amazon.com.mx",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class FakeContext:
        def __init__(self) -> None:
            self.cookies = None

        async def add_cookies(self, cookies):
            self.cookies = cookies

    class FakeBrowser:
        def __init__(self) -> None:
            self._context = FakeContext()
            self.started = 0

        async def _ensure_started(self):
            self.started += 1

    browser = FakeBrowser()
    session = AmazonSession.from_settings(cookies_path=str(cookies_path))
    health = await session.inject_into_browser(browser)

    assert health.loaded == 1
    assert browser.started == 1
    assert browser._context.cookies[0]["name"] == "sid"
