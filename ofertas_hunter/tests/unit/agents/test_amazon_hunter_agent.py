"""Tests del AmazonHunterAgent."""

from __future__ import annotations

from pathlib import Path

import pytest

from ofertas_hunter.agents.amazon_hunter_agent import AmazonHunterAgent
from ofertas_hunter.browser.browser_context import RenderedPage
from ofertas_hunter.db import connect, init_db


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "amazon"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


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


@pytest.mark.asyncio
async def test_amazon_hunter_creates_price_observation(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://www.amazon.com.mx/dp/B0EXAMPLEK"
    browser = FakeBrowserWorker({url: _ok(_load("jbl_normal_offer.html"), url)})
    agent = AmazonHunterAgent(browser=browser, db_conn=conn)

    outcomes = await agent.hunt_urls([url])
    assert len(outcomes) == 1
    out = outcomes[0]
    assert out.extracted is not None
    assert out.extracted.is_publishable is True

    rows = conn.execute(
        "SELECT current_price, previous_price, discount_percent, has_stock FROM price_observations"
    ).fetchall()
    assert len(rows) == 1
    obs = rows[0]
    assert obs["current_price"] == 388.0
    assert obs["previous_price"] == 899.0
    assert obs["has_stock"] == 1
    assert obs["discount_percent"] >= 56  # cálculo real ~56.84

    products = conn.execute("SELECT marketplace, marketplace_id, brand FROM products").fetchall()
    assert len(products) == 1
    assert products[0]["marketplace"] == "amazon"
    assert products[0]["marketplace_id"] == "B0EXAMPLEK"
    conn.close()


@pytest.mark.asyncio
async def test_amazon_hunter_enqueues_offer_over_50_percent(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://www.amazon.com.mx/dp/B0EXAMPLEK"
    browser = FakeBrowserWorker({url: _ok(_load("jbl_normal_offer.html"), url)})
    agent = AmazonHunterAgent(browser=browser, db_conn=conn)

    outcomes = await agent.hunt_urls([url])
    assert outcomes[0].enqueued_outbox_id is not None
    rows = conn.execute("SELECT type, state FROM outbox").fetchall()
    assert len(rows) == 1
    # 56.84% no es >= 50% por poco — verificamos
    assert rows[0]["type"] == "normal"
    assert rows[0]["state"] == "pending"
    conn.close()


@pytest.mark.asyncio
async def test_amazon_hunter_enqueues_price_error(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://www.amazon.com.mx/dp/B0EXAMPLEI"
    browser = FakeBrowserWorker({url: _ok(_load("iphone_extreme_low_price.html"), url)})
    agent = AmazonHunterAgent(browser=browser, db_conn=conn)

    outcomes = await agent.hunt_urls([url])
    assert outcomes[0].extracted is not None
    assert outcomes[0].extracted.discount_percent == 88
    rows = conn.execute("SELECT type, state, message_payload_json FROM outbox").fetchall()
    assert len(rows) == 1
    assert rows[0]["type"] == "price_error"
    assert "iPhone 16 Pro Max" in rows[0]["message_payload_json"]
    conn.close()


@pytest.mark.asyncio
async def test_amazon_hunter_discards_out_of_stock(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://www.amazon.com.mx/dp/B0EXAMPLEZ"
    browser = FakeBrowserWorker({url: _ok(_load("out_of_stock.html"), url)})
    agent = AmazonHunterAgent(browser=browser, db_conn=conn)

    outcomes = await agent.hunt_urls([url])
    assert outcomes[0].discarded_reason == "out_of_stock"
    discarded = conn.execute("SELECT reason FROM discarded_candidates").fetchall()
    assert len(discarded) == 1
    assert discarded[0]["reason"] == "out_of_stock"

    outbox_rows = conn.execute("SELECT count(*) as n FROM outbox").fetchone()
    assert outbox_rows["n"] == 0

    # Snapshot guardado
    snaps = conn.execute("SELECT context, reason FROM dom_snapshots").fetchall()
    assert len(snaps) == 1
    conn.close()


@pytest.mark.asyncio
async def test_amazon_hunter_dom_failure_saves_snapshot(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://www.amazon.com.mx/dp/B0BROKEN1"
    browser = FakeBrowserWorker({url: _ok(_load("dom_broken.html"), url)})
    agent = AmazonHunterAgent(browser=browser, db_conn=conn)

    outcomes = await agent.hunt_urls([url])
    assert outcomes[0].discarded_reason in ("no_title", "no_price", "no_image")
    snaps = conn.execute("SELECT reason FROM dom_snapshots").fetchall()
    assert any(s["reason"] == "not_publishable" for s in snaps)
    conn.close()


@pytest.mark.asyncio
async def test_amazon_hunter_handles_captcha(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://www.amazon.com.mx/dp/B0CAPTCHA"
    browser = FakeBrowserWorker(
        {
            url: RenderedPage(
                url=url,
                final_url=url,
                status=503,
                html="Enter the characters you see below",
                error="captcha_detected",
                blocked=True,
            )
        }
    )
    agent = AmazonHunterAgent(browser=browser, db_conn=conn)
    outcomes = await agent.hunt_urls([url])
    assert outcomes[0].discarded_reason == "captcha_detected"
    snaps = conn.execute("SELECT reason FROM dom_snapshots").fetchall()
    assert any(s["reason"] == "fetch_failed" for s in snaps)
    conn.close()



# ---------------------------------------------------------------------------
# Captcha guardrails (legacy audit / false positive prevention)
# ---------------------------------------------------------------------------


def _captcha_high_page(url: str) -> RenderedPage:
    """Página con captcha real high confidence (legacy-style)."""
    html = _load("real_captcha_continuar_comprando.html")
    return RenderedPage(
        url=url,
        final_url=url,
        status=200,
        html=html,
        error="captcha_detected",
        blocked=True,
        extras={
            "captcha_assessment": {
                "is_captcha": True,
                "confidence": "high",
                "strong_signals": [
                    "form_action_validate_captcha",
                ],
                "weak_signals": ["validateCaptcha"],
                "visible_signals": ["text_continuar_comprando"],
                "should_pause_marketplace": True,
                "reasons": ["body_tiny_with_amazon_title"],
            },
            "is_captcha": True,
            "confidence": "high",
            "strong_signals": ["form_action_validate_captcha"],
            "weak_signals": ["validateCaptcha"],
            "visible_signals": ["text_continuar_comprando"],
            "should_pause_marketplace": True,
        },
    )


def _captcha_low_page(url: str) -> RenderedPage:
    """Página de producto con substring `validateCaptcha` en script — falso positivo."""
    html = _load("false_positive_script_validatecaptcha.html")
    return RenderedPage(
        url=url,
        final_url=url,
        status=200,
        html=html,
        error=None,
        blocked=False,
        extras={
            "captcha_assessment": {
                "is_captcha": True,
                "confidence": "low",
                "strong_signals": [],
                "weak_signals": ["validateCaptcha", "amzn-captcha"],
                "visible_signals": [],
                "should_pause_marketplace": False,
                "reasons": ["weak_token_only_no_structure"],
            },
            "is_captcha": True,
            "confidence": "low",
            "strong_signals": [],
            "weak_signals": ["validateCaptcha", "amzn-captcha"],
            "should_pause_marketplace": False,
        },
    )


@pytest.mark.asyncio
async def test_amazon_hunter_emits_confirmed_event_on_high_confidence_captcha(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://www.amazon.com.mx/dp/B0CAPHIGH"
    browser = FakeBrowserWorker({url: _captcha_high_page(url)})
    agent = AmazonHunterAgent(browser=browser, db_conn=conn)

    outcomes = await agent.hunt_urls([url])
    assert outcomes[0].discarded_reason == "captcha_detected"
    assert outcomes[0].classification == "captcha"

    events = conn.execute(
        "SELECT kind, severity FROM runtime_events WHERE kind='amazon_captcha_confirmed'"
    ).fetchall()
    assert len(events) == 1
    assert events[0]["severity"] == "error"
    conn.close()


@pytest.mark.asyncio
async def test_amazon_hunter_does_not_pause_on_low_confidence_captcha(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://www.amazon.com.mx/dp/B0CAPLOW"
    page_low = _captcha_low_page(url)
    # En low confidence el browser no marca `blocked`. El agent debe parsear
    # la página y, como NO tiene precio extraíble, descartarla con razón
    # `not_publishable` (NO `captcha_detected`).
    browser = FakeBrowserWorker({url: page_low})
    agent = AmazonHunterAgent(browser=browser, db_conn=conn)

    outcomes = await agent.hunt_urls([url])
    # El low-confidence dejó pasar el HTML al parser. Como el HTML
    # tampoco produce un producto publicable (precio en español no
    # parseable), se descarta con razón distinta a captcha.
    assert outcomes[0].discarded_reason != "captcha_detected"

    # No se emitió `amazon_captcha_confirmed`.
    confirmed = conn.execute(
        "SELECT count(*) AS n FROM runtime_events WHERE kind='amazon_captcha_confirmed'"
    ).fetchone()["n"]
    assert confirmed == 0
    conn.close()


@pytest.mark.asyncio
async def test_amazon_hunter_emits_suspected_false_captcha_when_blocked_with_low_confidence(tmp_path):
    """Defensa: si el browser por error marca `blocked=True` pero la
    confianza es 'medium' o 'low', el agent emite
    `amazon_suspected_false_captcha` en lugar de `amazon_captcha_confirmed`.
    Para reproducirlo, simulamos un `RenderedPage` con `blocked=False` y
    `is_captcha=True confidence=medium`.
    """
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://www.amazon.com.mx/dp/B0SUSPECT"
    page = RenderedPage(
        url=url,
        final_url=url,
        status=503,
        html="",
        error="net::ERR_TIMED_OUT",
        blocked=False,
        extras={
            "captcha_assessment": {
                "is_captcha": True,
                "confidence": "medium",
                "strong_signals": ["url_validate_captcha"],
                "weak_signals": [],
                "visible_signals": [],
                "should_pause_marketplace": False,
                "reasons": ["medium_no_visible_text"],
            },
            "is_captcha": True,
            "confidence": "medium",
            "strong_signals": ["url_validate_captcha"],
            "weak_signals": [],
            "should_pause_marketplace": False,
        },
    )
    browser = FakeBrowserWorker({url: page})
    agent = AmazonHunterAgent(browser=browser, db_conn=conn)

    outcomes = await agent.hunt_urls([url])
    assert outcomes[0].discarded_reason == "amazon_extraction_failed"

    events = conn.execute(
        "SELECT kind FROM runtime_events WHERE kind='amazon_suspected_false_captcha'"
    ).fetchall()
    assert len(events) == 1

    confirmed = conn.execute(
        "SELECT count(*) AS n FROM runtime_events WHERE kind='amazon_captcha_confirmed'"
    ).fetchone()["n"]
    assert confirmed == 0
    conn.close()
