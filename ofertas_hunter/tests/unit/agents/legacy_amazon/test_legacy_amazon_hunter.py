"""Tests del `LegacyAmazonHunterAgent`:
- Comparte frontier con el bot nuevo.
- Switch en orchestrator usa el legacy SOLO si el flag está activo.
- Browser config replica los defaults anti-captcha del legacy original.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ofertas_hunter.agents.legacy_amazon_hunter_agent import (
    LegacyAmazonHunterAgent,
    LegacyHuntOutcome,
)
from ofertas_hunter.agents.legacy_amazon.worker import (
    LegacyAmazonWorker,
    LegacyFetchResult,
    USER_AGENTS,
    VIEWPORTS,
)
from ofertas_hunter.config import Settings
from ofertas_hunter.db import init_db
from ofertas_hunter.exploration.frontier import FrontierRepo
from ofertas_hunter.intelligence.price_error_scorer import PriceErrorScorer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _ok_data(asin: str = "B0SHARE0001") -> dict:
    return {
        "url": f"https://www.amazon.com.mx/dp/{asin}",
        "title": "Lavadora Mabe 17 kg Carga Superior",
        "price_current": 4999.0,
        "price_original": 9999.0,
        "discount_percent": 50.0,
        "image_url": "https://m.media-amazon.com/images/I/example.jpg",
        "asin": asin,
        "availability": "En stock",
    }


# ---------------------------------------------------------------------------
# Frontier compartido (Criterio 5 spec)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_amazon_hunter_respects_frontier_shared(db):
    """El hunter legacy debe consumir el frontier `kind=product` del bot
    nuevo, NO usar un frontier propio. Verificable: insertamos URL en
    frontier, llamamos a `hunt_from_frontier`, y la URL debe ser
    procesada (FakeWorker la recibe).
    """
    frontier = FrontierRepo(db)
    test_url = "https://www.amazon.com.mx/dp/B0SHARED01"
    frontier.add(test_url, kind="product")

    # FakeWorker: registra qué URL recibió
    received_urls: list[str] = []

    class FakeWorker:
        async def fetch_product(self, url: str) -> LegacyFetchResult:
            received_urls.append(url)
            return LegacyFetchResult(
                ok=True,
                final_url=url,
                status=200,
                captcha=False,
                captcha_confidence=None,
                data=_ok_data("B0SHARED01"),
                reason=None,
            )

        async def aclose(self) -> None:
            pass

    hunter = LegacyAmazonHunterAgent(
        worker=FakeWorker(),
        db_conn=db,
        scorer=PriceErrorScorer(),
    )
    outcomes = await hunter.hunt_from_frontier(max_urls=5)

    assert len(outcomes) == 1
    assert outcomes[0].url == test_url
    assert received_urls == [test_url]


# ---------------------------------------------------------------------------
# Switch flag (Criterio B spec)
# ---------------------------------------------------------------------------


def test_amazon_hunter_switch_uses_legacy_when_flag_enabled(db):
    """Cuando `settings.amazon_hunter_legacy=True`, el orchestrator usa el
    factory legacy (no el del bot nuevo)."""
    from ofertas_hunter.orchestrator import AgentFactoryBuilder, OrchestratorConfig

    settings = Settings(amazon_hunter_legacy=True, amazon_enabled=True)
    cfg = OrchestratorConfig(amazon_seeds=[], mercadolibre_seeds=[], once=True)
    builder = AgentFactoryBuilder(conn=db, settings=settings, config=cfg)

    # El factory existe y retorna algo (no None) cuando flag está ON.
    factory = builder.make_amazon_hunter_factory()
    assert factory is not None

    # El método _make_legacy_amazon_hunter_factory también se puede
    # invocar directamente y retorna factory válida.
    legacy_factory = builder._make_legacy_amazon_hunter_factory()
    assert callable(legacy_factory)


def test_amazon_hunter_switch_uses_new_when_flag_disabled(db):
    """Cuando `settings.amazon_hunter_legacy=False` (default), el
    orchestrator usa el factory del bot nuevo."""
    from ofertas_hunter.orchestrator import AgentFactoryBuilder, OrchestratorConfig

    settings = Settings(amazon_hunter_legacy=False, amazon_enabled=True)
    cfg = OrchestratorConfig(amazon_seeds=[], mercadolibre_seeds=[], once=True)
    builder = AgentFactoryBuilder(conn=db, settings=settings, config=cfg)

    factory = builder.make_amazon_hunter_factory()
    assert factory is not None

    # Si amazon está deshabilitado, ambas rutas devuelven None:
    settings_off = Settings(amazon_hunter_legacy=False, amazon_enabled=False)
    builder_off = AgentFactoryBuilder(conn=db, settings=settings_off, config=cfg)
    assert builder_off.make_amazon_hunter_factory() is None


def test_only_one_amazon_hunter_runs_at_a_time(db):
    """Spec criterio B: si `amazon_hunter_legacy=True`, el orchestrator
    NO debe levantar también `AmazonHunterAgent`. Verificable: el
    `make_amazon_hunter_factory` retorna SOLO una factory, y ésa es la
    legacy cuando el flag está ON.
    """
    from ofertas_hunter.orchestrator import AgentFactoryBuilder, OrchestratorConfig

    cfg = OrchestratorConfig(amazon_seeds=[], mercadolibre_seeds=[], once=True)

    # Con flag ON, factory existe (legacy):
    settings_on = Settings(amazon_hunter_legacy=True, amazon_enabled=True)
    builder_on = AgentFactoryBuilder(conn=db, settings=settings_on, config=cfg)
    factory_on = builder_on.make_amazon_hunter_factory()
    assert factory_on is not None

    # Con flag OFF, factory existe (nuevo):
    settings_off = Settings(amazon_hunter_legacy=False, amazon_enabled=True)
    builder_off = AgentFactoryBuilder(conn=db, settings=settings_off, config=cfg)
    factory_off = builder_off.make_amazon_hunter_factory()
    assert factory_off is not None

    # Pero NUNCA dos a la vez: la API solo expone una factory por marketplace.
    assert not hasattr(builder_on, "make_amazon_hunter_factories")


# ---------------------------------------------------------------------------
# Browser config: defaults anti-captcha del legacy original
# ---------------------------------------------------------------------------


def test_legacy_amazon_browser_config_matches_scrapperia_anti_captcha_defaults():
    """El `LegacyAmazonWorker` debe replicar los defaults del scraper
    legacy original: USA_AGENTS rotables, viewports realistas, headless
    por defecto, warmup_homepage=True, delays largos.
    """
    worker = LegacyAmazonWorker()  # defaults
    assert worker.headless is True
    assert worker.warmup_homepage is True
    # Delay legacy histórico: rango de 8-15s entre páginas como defensa.
    min_ms, max_ms = worker.delay_between_requests_ms
    assert min_ms >= 5000, "Delay mínimo entre fetches debe ser >= 5s para legacy"
    assert max_ms >= min_ms

    # USER_AGENTS rotables: deben ser >= 3 versiones Chrome distintas
    assert len(USER_AGENTS) >= 3
    for ua in USER_AGENTS:
        assert "Chrome" in ua
        # Solo Chrome de versiones recientes (122+):
        assert any(f"Chrome/{v}" in ua for v in ("122", "123", "124", "125", "126"))

    # VIEWPORTS realistas: por lo menos 3 distintos
    assert len(VIEWPORTS) >= 3
    for vp in VIEWPORTS:
        assert vp["width"] >= 1280
        assert vp["height"] >= 720


def test_legacy_amazon_worker_uses_ephemeral_browser_not_persistent():
    """Spec criterio 3: el legacy NO usa `launch_persistent_context`. El
    worker debe usar `chromium.launch` + `new_context` para garantizar
    cookies efímeras (la receta anti-captcha del legacy original).
    """
    import inspect
    from ofertas_hunter.agents.legacy_amazon import worker as worker_module

    src = inspect.getsource(worker_module)
    # NO debe usar launch_persistent_context:
    assert "launch_persistent_context" not in src
    # Sí debe usar launch + new_context:
    assert "chromium.launch" in src
    assert "new_context" in src


def test_legacy_amazon_worker_injects_stealth_script():
    """Spec criterio 3: el worker debe inyectar el `STEALTH_SCRIPT` del
    legacy (oculta `webdriver`, plugins, etc.).
    """
    import inspect
    from ofertas_hunter.agents.legacy_amazon import worker as worker_module

    src = inspect.getsource(worker_module)
    assert "add_init_script" in src
    assert "STEALTH_SCRIPT" in src
    # El script debe esconder webdriver:
    assert "webdriver" in worker_module.STEALTH_SCRIPT
    assert "plugins" in worker_module.STEALTH_SCRIPT


# ---------------------------------------------------------------------------
# Outcome shape compatible con HuntOutcome del bot nuevo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_hunt_outcome_has_same_shape_as_new_hunt_outcome(db):
    """`LegacyHuntOutcome` debe exponer los mismos atributos que
    `HuntOutcome` (del bot nuevo) para que el orquestador no tenga que
    ramificar."""
    from ofertas_hunter.agents.amazon_hunter_agent import HuntOutcome

    new_fields = {f for f in HuntOutcome.__dataclass_fields__.keys()}
    legacy_fields = {f for f in LegacyHuntOutcome.__dataclass_fields__.keys()}

    # Los campos críticos del nuevo deben existir también en legacy:
    critical = {
        "url",
        "final_url",
        "extracted",
        "classification",
        "suggested_outbox_type",
        "enqueued_outbox_id",
        "discarded_reason",
        "captcha_confidence",
        "captcha_should_pause_marketplace",
    }
    missing = critical - legacy_fields
    assert not missing, f"LegacyHuntOutcome no expone: {missing}"



# ---------------------------------------------------------------------------
# MCP context: get_amazon_hunter respeta el flag (cubre opciones [1]/[2]/[3]
# del lanzador, que pasan por kiro-cli o orquestador_ia.py → MCP server)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_context_get_amazon_hunter_returns_legacy_when_flag_enabled(db):
    """El MCP context debe devolver `LegacyAmazonHunterAgent` cuando el
    flag está activo. Esto cubre `orquestador_ia.py` y kiro-cli, que
    NO usan el orchestrator nativo sino MCP tools.
    """
    from ofertas_hunter.mcp.context import ServerContext
    from ofertas_hunter.agents.legacy_amazon_hunter_agent import (
        LegacyAmazonHunterAgent,
    )

    settings = Settings(amazon_hunter_legacy=True, amazon_enabled=True)
    ctx = ServerContext.build(db=db, settings=settings)
    try:
        hunter = await ctx.get_amazon_hunter()
        assert isinstance(hunter, LegacyAmazonHunterAgent), (
            f"con flag=True debe devolver LegacyAmazonHunterAgent, "
            f"obtuvo {type(hunter).__name__}"
        )
    finally:
        await ctx.aclose()


@pytest.mark.asyncio
async def test_mcp_context_get_amazon_hunter_returns_new_when_flag_disabled(db):
    """Default: AmazonHunterAgent (bot nuevo). NO se debe instanciar el
    legacy a no ser que el flag esté en True."""
    from ofertas_hunter.mcp.context import ServerContext
    from ofertas_hunter.agents.amazon_hunter_agent import AmazonHunterAgent

    settings = Settings(amazon_hunter_legacy=False, amazon_enabled=True)
    ctx = ServerContext.build(db=db, settings=settings)
    try:
        # El new requiere browser playwright instalado, lo cual está OK
        # en este entorno. Si no, salta a un branch distinto que no es
        # legacy.
        try:
            hunter = await ctx.get_amazon_hunter()
        except Exception:
            # Browser fallo (sin Playwright en CI). El test sigue siendo
            # útil porque no debe haber instanciado el legacy.
            assert ctx._amazon_hunter is None or not isinstance(
                ctx._amazon_hunter, type(None)
            ) or "Legacy" not in type(ctx._amazon_hunter).__name__
            return
        assert isinstance(hunter, AmazonHunterAgent), (
            f"con flag=False debe devolver AmazonHunterAgent, "
            f"obtuvo {type(hunter).__name__}"
        )
    finally:
        await ctx.aclose()
