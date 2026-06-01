from __future__ import annotations

from pathlib import Path

import pytest

from ofertas_hunter.browser.browser_context import BrowserConfig
from ofertas_hunter.browser.playwright_worker import PlaywrightBrowserWorker


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "amazon"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class _FakeMouse:
    async def move(self, _x, _y) -> None:
        return None


class _FakePage:
    def __init__(self, *, final_url: str, status: int, html: str) -> None:
        self.final_url = final_url
        self._status = status
        self._html = html
        self.closed = False
        self.url = final_url
        self.viewport_size = {"width": 1366, "height": 768}
        self.mouse = _FakeMouse()

    async def goto(self, _url: str, **_kwargs):
        return _FakeResponse(self._status)

    async def content(self) -> str:
        return self._html

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def screenshot(self, full_page: bool = False):  # noqa: ARG002
        return b"png"

    async def evaluate(self, _script: str):
        return 1600

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, retry_pages: list[_FakePage]) -> None:
        self.retry_pages = retry_pages

    async def new_page(self):
        if not self.retry_pages:
            raise AssertionError("no retry pages configured")
        return self.retry_pages.pop(0)


class _TestableWorker(PlaywrightBrowserWorker):
    def __init__(self, primary_page: _FakePage, retry_pages: list[_FakePage]) -> None:
        super().__init__(BrowserConfig())
        self._page = primary_page
        self._context = _FakeContext(retry_pages)

    async def _ensure_started(self) -> None:  # pragma: no cover
        return None

    async def _get_or_create_page(self):
        return self._page

    async def _simulate_human_browsing(self, page) -> None:  # noqa: ARG002
        return None

    def _captcha_retry_wait_seconds(self) -> float:
        return 0.0


@pytest.mark.asyncio
async def test_worker_retries_once_on_high_confidence_captcha_and_recovers():
    url = "https://www.amazon.com.mx/dp/B010ASII32"
    primary = _FakePage(
        final_url=url,
        status=200,
        html=_load("real_captcha_continuar_comprando.html"),
    )
    retry = _FakePage(
        final_url=f"{url}?th=1",
        status=200,
        html=_load("jbl_normal_offer.html"),
    )
    worker = _TestableWorker(primary, [retry])

    result = await worker._do_fetch(url)  # noqa: SLF001

    assert result.blocked is False
    assert result.error is None
    assert result.final_url.endswith("?th=1")
    assert "JBL" in result.html


@pytest.mark.asyncio
async def test_worker_keeps_captcha_when_retry_also_hits_challenge():
    url = "https://www.amazon.com.mx/dp/B010ASII32"
    primary = _FakePage(
        final_url=url,
        status=200,
        html=_load("real_captcha_continuar_comprando.html"),
    )
    retry = _FakePage(
        final_url=url,
        status=200,
        html=_load("real_captcha_continuar_comprando.html"),
    )
    worker = _TestableWorker(primary, [retry])

    result = await worker._do_fetch(url)  # noqa: SLF001

    assert result.blocked is True
    assert result.error == "captcha_detected"
    assert result.extras.get("confidence") == "high"
