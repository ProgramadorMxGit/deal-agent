"""Tests del MercadoLibreHunterAgent."""

from __future__ import annotations

from pathlib import Path

import pytest

from ofertas_hunter.agents.mercadolibre_hunter_agent import (
    MercadoLibreHunterAgent,
    MlHuntOutcome,
)
from ofertas_hunter.browser.browser_context import RenderedPage
from ofertas_hunter.db import connect, init_db
from ofertas_hunter.marketplaces.mercadolibre_affiliate import (
    AffiliateInfo,
    AffiliateExtractor,
)


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "mercadolibre"


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


class FakeAffiliateExtractor:
    """Extractor en memoria que devuelve un link configurable.

    Si `error` está set, devuelve `success=False`.
    """

    def __init__(
        self,
        *,
        affiliate_url: str = "https://meli.la/abc123",
        affiliate_product_id: str = "FAKE-12345",
        commission_text: str = "COMISIÓN 9%",
        error: str | None = None,
    ) -> None:
        self.affiliate_url = affiliate_url
        self.affiliate_product_id = affiliate_product_id
        self.commission_text = commission_text
        self.error = error
        self.calls: list[str] = []

    async def extract(self, url: str) -> AffiliateInfo:
        self.calls.append(url)
        if self.error:
            return AffiliateInfo(success=False, error=self.error)
        return AffiliateInfo(
            affiliate_url=self.affiliate_url,
            affiliate_product_id=self.affiliate_product_id,
            commission_text=self.commission_text,
            success=True,
        )

    async def aclose(self) -> None:  # pragma: no cover
        pass


def _ok(html: str, url: str) -> RenderedPage:
    return RenderedPage(url=url, final_url=url, status=200, html=html)


@pytest.mark.asyncio
async def test_ml_hunter_creates_price_observation(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://articulo.mercadolibre.com.mx/MLM98765432-sony"
    browser = FakeBrowserWorker({url: _ok(_load("extreme_discount_60_percent.html"), url)})
    agent = MercadoLibreHunterAgent(
        browser=browser, db_conn=conn,
        affiliate_extractor=FakeAffiliateExtractor(),
    )

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
    assert obs["current_price"] == 3499.0
    assert obs["previous_price"] == 8999.0
    assert obs["has_stock"] == 1
    assert obs["discount_percent"] == 61

    products = conn.execute(
        "SELECT marketplace, marketplace_id, condition, affiliate_link FROM products"
    ).fetchall()
    assert len(products) == 1
    assert products[0]["marketplace"] == "mercadolibre"
    assert products[0]["marketplace_id"] == "MLM98765432"
    assert products[0]["condition"] == "new"
    assert products[0]["affiliate_link"] == "https://meli.la/abc123"
    conn.close()


@pytest.mark.asyncio
async def test_ml_hunter_enqueues_offer_over_50_percent(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://articulo.mercadolibre.com.mx/MLM98765432-sony"
    browser = FakeBrowserWorker({url: _ok(_load("extreme_discount_60_percent.html"), url)})
    agent = MercadoLibreHunterAgent(
        browser=browser, db_conn=conn,
        affiliate_extractor=FakeAffiliateExtractor(),
    )

    outcomes = await agent.hunt_urls([url])
    assert outcomes[0].enqueued_outbox_id is not None
    rows = conn.execute("SELECT type, state, message_payload_json FROM outbox").fetchall()
    assert len(rows) == 1
    assert rows[0]["type"] in ("normal", "price_error", "possible_pe")
    assert rows[0]["state"] == "pending"
    # Affiliate metadata persistida en el payload
    assert "https://meli.la/abc123" in rows[0]["message_payload_json"]
    conn.close()


@pytest.mark.asyncio
async def test_ml_hunter_enqueues_price_error(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://articulo.mercadolibre.com.mx/MLM98765432-sony"
    browser = FakeBrowserWorker({url: _ok(_load("extreme_discount_60_percent.html"), url)})
    agent = MercadoLibreHunterAgent(
        browser=browser, db_conn=conn,
        affiliate_extractor=FakeAffiliateExtractor(),
    )
    outcomes = await agent.hunt_urls([url])
    assert outcomes[0].enqueued_outbox_id is not None
    rows = conn.execute("SELECT type FROM outbox").fetchone()
    assert rows["type"] in ("normal", "price_error", "possible_pe")
    conn.close()


@pytest.mark.asyncio
async def test_ml_hunter_discards_used_low_discount(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://articulo.mercadolibre.com.mx/MLM11111111-canon-eos"
    browser = FakeBrowserWorker({url: _ok(_load("used_low_discount.html"), url)})
    agent = MercadoLibreHunterAgent(
        browser=browser, db_conn=conn,
        affiliate_extractor=FakeAffiliateExtractor(),
    )
    outcomes = await agent.hunt_urls([url])
    assert outcomes[0].discarded_reason == "used_requires_extreme_discount"
    discarded = conn.execute("SELECT reason FROM discarded_candidates").fetchall()
    assert any(d["reason"] == "used_requires_extreme_discount" for d in discarded)
    rows = conn.execute("SELECT count(*) AS n FROM outbox").fetchone()
    assert rows["n"] == 0
    conn.close()


@pytest.mark.asyncio
async def test_ml_hunter_dom_failure_saves_snapshot(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://articulo.mercadolibre.com.mx/MLM22222222-broken"
    browser = FakeBrowserWorker({url: _ok(_load("dom_broken.html"), url)})
    agent = MercadoLibreHunterAgent(
        browser=browser, db_conn=conn,
        affiliate_extractor=FakeAffiliateExtractor(),
    )
    outcomes = await agent.hunt_urls([url])
    assert outcomes[0].discarded_reason in ("no_title", "no_price", "no_image")
    snaps = conn.execute("SELECT reason FROM dom_snapshots").fetchall()
    assert any(s["reason"] == "not_publishable" for s in snaps)
    conn.close()


@pytest.mark.asyncio
async def test_ml_hunter_pauses_on_login_redirect(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://articulo.mercadolibre.com.mx/MLM55555555-foo"
    final_url = "https://www.mercadolibre.com.mx/gz/account-verification?next=/MLM55555555"
    browser = FakeBrowserWorker(
        {
            url: RenderedPage(
                url=url, final_url=final_url, status=200, html="<html>login</html>"
            )
        }
    )
    agent = MercadoLibreHunterAgent(browser=browser, db_conn=conn)
    outcomes = await agent.hunt_urls([url, url + "/two", url + "/three"])
    assert len(outcomes) == 1
    assert outcomes[0].paused_for_login is True
    assert agent.paused is True
    rows = conn.execute("SELECT kind, severity FROM runtime_events").fetchall()
    assert any(r["kind"] == "cookie_expiry" and r["severity"] == "critical" for r in rows)
    conn.close()


@pytest.mark.asyncio
async def test_ml_hunter_handles_captcha(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://articulo.mercadolibre.com.mx/MLM33333333-blocked"
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
    agent = MercadoLibreHunterAgent(browser=browser, db_conn=conn)
    outcomes = await agent.hunt_urls([url])
    assert outcomes[0].discarded_reason == "captcha_detected"
    assert outcomes[0].paused_for_login is True
    rows = conn.execute("SELECT kind FROM runtime_events").fetchall()
    assert any(r["kind"] == "captcha" for r in rows)
    conn.close()


# ---------------------------------------------------------------------------
# Tests obligatorios de afiliados (regla dura Fase 3.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ml_hunter_requests_affiliate_before_outbox(tmp_path):
    """El hunter debe llamar al affiliate_extractor para cada producto publicable."""
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://articulo.mercadolibre.com.mx/MLM98765432-sony"
    browser = FakeBrowserWorker({url: _ok(_load("extreme_discount_60_percent.html"), url)})
    extractor = FakeAffiliateExtractor()
    agent = MercadoLibreHunterAgent(
        browser=browser, db_conn=conn, affiliate_extractor=extractor
    )

    outcomes = await agent.hunt_urls([url])
    # El extractor recibió la canonical_url (con slug preservado).
    assert extractor.calls == ["https://articulo.mercadolibre.com.mx/MLM98765432-sony"]
    assert outcomes[0].affiliate_url == "https://meli.la/abc123"
    assert outcomes[0].affiliate_product_id == "FAKE-12345"
    conn.close()


@pytest.mark.asyncio
async def test_ml_outbox_payload_contains_affiliate_url(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    url = "https://articulo.mercadolibre.com.mx/MLM98765432-sony"
    browser = FakeBrowserWorker({url: _ok(_load("extreme_discount_60_percent.html"), url)})
    agent = MercadoLibreHunterAgent(
        browser=browser, db_conn=conn,
        affiliate_extractor=FakeAffiliateExtractor(),
    )
    await agent.hunt_urls([url])

    import json

    row = conn.execute("SELECT message_payload_json FROM outbox").fetchone()
    payload = json.loads(row["message_payload_json"])
    assert payload["affiliate_url"] == "https://meli.la/abc123"
    assert payload["affiliate_product_id"] == "FAKE-12345"
    assert payload["commission_text"] == "COMISIÓN 9%"
    assert payload["brand"]
    assert "category" in payload
    # canonical_url se preserva para scraping (ahora incluye el slug)
    assert payload["canonical_url"] == "https://articulo.mercadolibre.com.mx/MLM98765432-sony"
    # url (publicación) usa affiliate_url
    assert payload["url"] == "https://meli.la/abc123"
    conn.close()


@pytest.mark.asyncio
async def test_ml_canonical_url_is_used_for_revalidation(tmp_path):
    """Aunque url sea affiliate_url, canonical_url permanece para revalidar."""
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    url = "https://articulo.mercadolibre.com.mx/MLM98765432-sony"
    browser = FakeBrowserWorker({url: _ok(_load("extreme_discount_60_percent.html"), url)})
    agent = MercadoLibreHunterAgent(
        browser=browser, db_conn=conn,
        affiliate_extractor=FakeAffiliateExtractor(),
    )
    await agent.hunt_urls([url])

    import json

    row = conn.execute("SELECT message_payload_json FROM outbox").fetchone()
    payload = json.loads(row["message_payload_json"])
    assert payload["canonical_url"].startswith("https://articulo.mercadolibre.com.mx/MLM98765432")
    assert "meli.la" not in payload["canonical_url"]


@pytest.mark.asyncio
async def test_ml_missing_affiliate_blocks_publication(tmp_path):
    """Si falta affiliate_url y affiliate_required=True, item NO entra al outbox."""
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://articulo.mercadolibre.com.mx/MLM98765432-sony"
    browser = FakeBrowserWorker({url: _ok(_load("extreme_discount_60_percent.html"), url)})

    # Extractor que falla (modal vacío, share button ausente, etc.)
    failing = FakeAffiliateExtractor(error="modal_textareas_empty")
    failing.affiliate_url = None
    failing.affiliate_product_id = None
    agent = MercadoLibreHunterAgent(
        browser=browser, db_conn=conn,
        affiliate_extractor=failing,
        affiliate_required_for_publish=True,
    )
    outcomes = await agent.hunt_urls([url])
    assert outcomes[0].discarded_reason == "missing_affiliate_url"
    assert outcomes[0].affiliate_error == "modal_textareas_empty"

    rows = conn.execute("SELECT count(*) AS n FROM outbox").fetchone()
    assert rows["n"] == 0
    discarded = conn.execute("SELECT reason FROM discarded_candidates").fetchall()
    assert any(d["reason"] == "missing_affiliate_url" for d in discarded)
    conn.close()


@pytest.mark.asyncio
async def test_ml_missing_affiliate_allowed_when_not_required(tmp_path):
    """Con affiliate_required=False, un producto sin afiliado entra al outbox."""
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://articulo.mercadolibre.com.mx/MLM98765432-sony"
    browser = FakeBrowserWorker({url: _ok(_load("extreme_discount_60_percent.html"), url)})
    failing = FakeAffiliateExtractor(error="no_share_button")
    agent = MercadoLibreHunterAgent(
        browser=browser, db_conn=conn,
        affiliate_extractor=failing,
        affiliate_required_for_publish=False,
    )
    outcomes = await agent.hunt_urls([url])
    assert outcomes[0].enqueued_outbox_id is not None
    conn.close()


@pytest.mark.asyncio
async def test_ml_no_share_button_skips_affiliate_extraction(tmp_path):
    """Si la página no tiene share button, el hunter no llama al extractor
    real; devuelve AffiliateInfo(error='no_share_button') sintético.

    Como `extreme_discount_60_percent.html` ya trae share_button, removemos
    a propósito el campo en la versión parseada montando un parser fake. El
    camino real ya queda probado por
    `test_ml_missing_affiliate_blocks_publication`. Aquí solo confirmamos el
    flujo de _maybe_extract_affiliate cuando share_button está ausente.
    """
    from ofertas_hunter.agents.mercadolibre_hunter_agent import (
        MercadoLibreHunterAgent,
    )
    from ofertas_hunter.extraction.mercadolibre_product_parser import MercadoLibreProductParser
    from ofertas_hunter.marketplaces.base import ExtractedProduct

    extractor = FakeAffiliateExtractor()
    agent = MercadoLibreHunterAgent(
        browser=FakeBrowserWorker({}),
        affiliate_extractor=extractor,
        affiliate_required_for_publish=True,
    )

    fake_product = ExtractedProduct(
        url="https://articulo.mercadolibre.com.mx/MLM77777777-foo",
        canonical_url="https://articulo.mercadolibre.com.mx/MLM77777777",
        marketplace="mercadolibre",
        title="Producto sin share button",
        current_price=100.0,
        image_url="https://x.com/img.jpg",
    )
    fake_product.selected_variant_signals = {"share_button": False}

    info = await agent._maybe_extract_affiliate(fake_product)
    assert info is not None
    assert info.success is False
    assert info.error == "no_share_button"
    assert extractor.calls == []  # nunca se llamó


# ---------------------------------------------------------------------------
# Gate: session_manager
# ---------------------------------------------------------------------------


class _StubManagerInvalid:
    """Stub mínimo que reporta status != VALID."""

    class _Status:
        value = "invalid"

    status = _Status()

    def mark_invalid(self, reason):  # pragma: no cover
        pass


class _StubManagerValid:
    class _Status:
        value = "valid"

    status = _Status()

    def mark_invalid(self, reason):  # pragma: no cover
        pass


@pytest.mark.asyncio
async def test_ml_hunter_skips_when_session_manager_not_valid(tmp_path):
    """Test obligatorio E (parte ML): si el session_manager reporta
    cualquier estado != VALID, el hunter ML debe saltar el ciclo (sin
    afectar otros marketplaces). El bot main no debería llamarlo, pero
    si lo hace, queremos no-op + runtime_event.
    """
    db_path = tmp_path / "skip.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://articulo.mercadolibre.com.mx/MLM-skip"
    browser = FakeBrowserWorker({})
    agent = MercadoLibreHunterAgent(
        browser=browser,
        db_conn=conn,
        session_manager=_StubManagerInvalid(),
    )

    outcomes = await agent.hunt_urls([url])
    assert outcomes == []  # no se procesa ninguna URL

    # Debe haber runtime_event "ml_hunt_skipped_session_invalid"
    rows = conn.execute(
        "SELECT 1 FROM runtime_events WHERE kind = 'ml_hunt_skipped_session_invalid'"
    ).fetchall()
    assert len(rows) == 1
    conn.close()


@pytest.mark.asyncio
async def test_ml_hunter_runs_when_session_manager_is_valid(tmp_path):
    """Si el manager está VALID, el hunter procede normal."""
    db_path = tmp_path / "ok.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://articulo.mercadolibre.com.mx/MLM98765432-sony"
    browser = FakeBrowserWorker(
        {url: _ok(_load("extreme_discount_60_percent.html"), url)}
    )
    agent = MercadoLibreHunterAgent(
        browser=browser,
        db_conn=conn,
        affiliate_extractor=FakeAffiliateExtractor(),
        session_manager=_StubManagerValid(),
    )

    outcomes = await agent.hunt_urls([url])
    assert len(outcomes) == 1
    assert outcomes[0].extracted is not None
    conn.close()
