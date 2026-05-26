"""Tests de la política de pausa Amazon basada en confidence + should_pause.

Cubre los criterios añadidos por el operador:

- `test_option3_does_not_pause_on_form_action_validate_captcha_alone`
- `test_option3_pauses_only_on_high_confidence_real_captcha`
- `test_amazon_captcha_form_action_alone_is_not_pause_worthy`
- `test_amazon_captcha_debug_snapshot_saved_for_option3`
- `test_amazon_legacy_scrapperia_fixture_not_marked_as_captcha`
- `test_no_duplicate_amazon_captcha_detectors_exist`
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ofertas_hunter.agents.amazon_hunter_agent import AmazonHunterAgent
from ofertas_hunter.browser.amazon_captcha_detector import AmazonCaptchaDetector
from ofertas_hunter.browser.browser_context import RenderedPage
from ofertas_hunter.db import connect, init_db
from ofertas_hunter.mcp.tools.action_tools import _summarize_outcome


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


def _captcha_high_real_page(url: str) -> RenderedPage:
    """Captcha real high-confidence: form action + visible 'Continuar'."""
    html = _load("real_captcha_continuar_comprando.html")
    detector = AmazonCaptchaDetector()
    a = detector.assess(html=html, final_url=url, status=200)
    return RenderedPage(
        url=url,
        final_url=url,
        status=200,
        html=html,
        error="captcha_detected",
        blocked=True,
        extras={
            "captcha_assessment": {
                "is_captcha": a.is_captcha,
                "confidence": a.confidence,
                "strong_signals": list(a.strong_signals),
                "visible_signals": list(a.visible_signals),
                "weak_signals": list(a.weak_signals),
                "should_pause_marketplace": a.should_pause_marketplace,
            },
            "is_captcha": a.is_captcha,
            "confidence": a.confidence,
            "strong_signals": list(a.strong_signals),
            "visible_signals": list(a.visible_signals),
            "weak_signals": list(a.weak_signals),
            "should_pause_marketplace": a.should_pause_marketplace,
        },
    )


def _captcha_form_only_no_visible_page(url: str) -> RenderedPage:
    """Sólo `<form action="/errors/validateCaptcha">` SIN texto visible.

    El detector lo clasifica como `confidence=medium` y
    `should_pause_marketplace=False`. La opción 3 NO debe pausar.
    """
    html = (
        '<html><head><title>Producto X - Amazon.com.mx</title></head>'
        '<body><div id="dp-container"><h1 id="productTitle">Producto X</h1>'
        '<div hidden><form action="/errors/validateCaptcha"></form></div>'
        "</div></body></html>"
    )
    detector = AmazonCaptchaDetector()
    a = detector.assess(html=html, final_url=url, status=200)
    # Esperamos confidence=medium (estructura sin visible).
    assert a.is_captcha is True
    assert a.confidence == "medium"
    assert a.should_pause_marketplace is False
    return RenderedPage(
        url=url,
        final_url=url,
        status=200,
        html=html,
        error=None,  # browser NO marca blocked: confidence != high.
        blocked=False,
        extras={
            "captcha_assessment": {
                "is_captcha": True,
                "confidence": "medium",
                "strong_signals": list(a.strong_signals),
                "visible_signals": list(a.visible_signals),
                "weak_signals": list(a.weak_signals),
                "should_pause_marketplace": False,
                "reasons": list(a.reasons),
            },
            "is_captcha": True,
            "confidence": "medium",
            "strong_signals": list(a.strong_signals),
            "visible_signals": [],
            "weak_signals": list(a.weak_signals),
            "should_pause_marketplace": False,
        },
    )


# ---------------------------------------------------------------------------
# Detector: form_action solo no es pause-worthy
# ---------------------------------------------------------------------------


def test_amazon_captcha_form_action_alone_is_not_pause_worthy():
    """`<form action="/errors/validateCaptcha">` aislado, sin texto visible
    ni URL `/errors/validateCaptcha`, NO debe disparar pausa.
    """
    html = (
        '<html><head><title>Amazon.com.mx</title></head>'
        '<body><div hidden><form action="/errors/validateCaptcha"></form></div>'
        "</body></html>"
    )
    a = AmazonCaptchaDetector().assess(html=html, final_url="https://www.amazon.com.mx/dp/B0X")
    assert a.confidence in ("medium", "low")
    assert a.should_pause_marketplace is False


# ---------------------------------------------------------------------------
# Agente: HuntOutcome lleva la info necesaria
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amazon_hunter_outcome_carries_captcha_assessment(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    url = "https://www.amazon.com.mx/dp/B0FORMONLY"
    browser = FakeBrowserWorker({url: _captcha_form_only_no_visible_page(url)})
    agent = AmazonHunterAgent(browser=browser, db_conn=conn)
    outcomes = await agent.hunt_urls([url])
    out = outcomes[0]
    # Outcome NO debe marcar should_pause aunque el detector haya visto form_action.
    assert out.captcha_should_pause_marketplace is False
    assert out.captcha_confidence in ("medium", "low")
    assert "form_action_validate_captcha" in out.captcha_strong_signals
    assert out.captcha_visible_signals == ()
    # Discard reason NO debe ser captcha_detected (es medium → extraction_failed).
    assert out.discarded_reason != "captcha_detected"
    conn.close()


@pytest.mark.asyncio
async def test_amazon_hunter_outcome_high_confidence_marks_should_pause(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    url = "https://www.amazon.com.mx/dp/B0HIGH001"
    browser = FakeBrowserWorker({url: _captcha_high_real_page(url)})
    agent = AmazonHunterAgent(browser=browser, db_conn=conn)
    outcomes = await agent.hunt_urls([url])
    out = outcomes[0]
    assert out.discarded_reason == "captcha_detected"
    assert out.captcha_confidence == "high"
    assert out.captcha_should_pause_marketplace is True
    conn.close()


@pytest.mark.asyncio
async def test_amazon_captcha_debug_snapshot_saved_for_option3(tmp_path, monkeypatch):
    """Cuando el agente detecta captcha (real o sospechoso), persiste el
    HTML, screenshot y metadatos en `data/debug/amazon_captcha/`.
    """
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    url = "https://www.amazon.com.mx/dp/B0HIGHDEBUG"
    page = _captcha_high_real_page(url)
    # Agregamos screenshot bytes para validar que se persisten.
    page = RenderedPage(
        url=page.url,
        final_url=page.final_url,
        status=page.status,
        html=page.html,
        screenshot_bytes=b"\x89PNG\r\n\x1a\nfakepng",
        error=page.error,
        blocked=page.blocked,
        extras=page.extras,
    )
    browser = FakeBrowserWorker({url: page})
    agent = AmazonHunterAgent(browser=browser, db_conn=conn)
    outcomes = await agent.hunt_urls([url])
    out = outcomes[0]
    assert out.captcha_debug_path is not None

    debug_dir = tmp_path / "data" / "debug" / "amazon_captcha"
    files = list(debug_dir.glob("*"))
    htmls = [f for f in files if f.suffix == ".html"]
    metas = [f for f in files if f.suffix == ".json"]
    pngs = [f for f in files if f.suffix == ".png"]
    assert htmls and metas and pngs, f"no debug artifacts: {files}"
    meta = json.loads(metas[0].read_text(encoding="utf-8"))
    assert meta["url"] == url
    assert meta["captcha_assessment"]["confidence"] == "high"
    conn.close()


# ---------------------------------------------------------------------------
# Resumen MCP: debe exponer captcha_should_pause_marketplace
# ---------------------------------------------------------------------------


def test_summarize_outcome_exposes_captcha_fields():
    """El resumen que reciben los loops paralelos (orquestador_ia) debe
    incluir los campos de captcha. Si no, la opción 3 no puede distinguir
    high de medium y vuelve a pausar mal.
    """
    from ofertas_hunter.agents.amazon_hunter_agent import HuntOutcome

    outcome = HuntOutcome(
        url="u",
        final_url="u",
        extracted=None,
        classification="captcha",
        suggested_outbox_type=None,
        enqueued_outbox_id=None,
        discarded_reason="captcha_detected",
        captcha_confidence="high",
        captcha_should_pause_marketplace=True,
        captcha_strong_signals=("form_action_validate_captcha",),
        captcha_visible_signals=("text_continuar_comprando",),
    )
    summary = _summarize_outcome(outcome)
    assert summary["captcha_confidence"] == "high"
    assert summary["captcha_should_pause_marketplace"] is True
    assert "form_action_validate_captcha" in summary["captcha_strong_signals"]


# ---------------------------------------------------------------------------
# Política de pausa de orquestador_ia.loop_amazon
# ---------------------------------------------------------------------------


def _decide_pause(outcomes: list[dict]) -> str:
    """Replica la lógica de `loop_amazon` para tests unitarios.

    Devuelve "pause_long" si pausa global, "backoff_short" si 1 captcha
    real, "no_pause" si ninguno.
    """
    real_high = [
        o for o in outcomes
        if o.get("captcha_should_pause_marketplace") is True
        and (o.get("captcha_confidence") or "") == "high"
    ]
    if len(real_high) >= 2:
        return "pause_long"
    if len(real_high) == 1:
        return "backoff_short"
    return "no_pause"


def test_option3_does_not_pause_on_form_action_validate_captcha_alone():
    """5 outcomes con `discarded_reason=captcha_detected` pero confidence=medium
    NO deben disparar pausa global."""
    outcomes = [
        {
            "url": f"https://www.amazon.com.mx/dp/B0X{i}",
            "discarded_reason": "captcha_detected",
            "captcha_confidence": "medium",
            "captcha_should_pause_marketplace": False,
            "captcha_strong_signals": ["form_action_validate_captcha"],
            "captcha_visible_signals": [],
        }
        for i in range(5)
    ]
    assert _decide_pause(outcomes) == "no_pause"


def test_option3_pauses_only_on_high_confidence_real_captcha():
    """Sólo se pausa con >=2 captchas REALES high-confidence."""
    outcomes = [
        {
            "url": f"https://www.amazon.com.mx/dp/B0X{i}",
            "discarded_reason": "captcha_detected",
            "captcha_confidence": "high",
            "captcha_should_pause_marketplace": True,
            "captcha_strong_signals": ["form_action_validate_captcha"],
            "captcha_visible_signals": ["text_continuar_comprando"],
        }
        for i in range(2)
    ]
    assert _decide_pause(outcomes) == "pause_long"


def test_option3_one_high_captcha_only_backoff_short():
    outcomes = [
        {
            "url": "https://www.amazon.com.mx/dp/B0Y",
            "discarded_reason": "captcha_detected",
            "captcha_confidence": "high",
            "captcha_should_pause_marketplace": True,
            "captcha_strong_signals": ["form_action_validate_captcha"],
            "captcha_visible_signals": ["text_continuar_comprando"],
        },
        {
            "url": "https://www.amazon.com.mx/dp/B0Z",
            "discarded_reason": "amazon_extraction_failed",
            "captcha_confidence": "medium",
            "captcha_should_pause_marketplace": False,
            "captcha_strong_signals": ["form_action_validate_captcha"],
            "captcha_visible_signals": [],
        },
    ]
    assert _decide_pause(outcomes) == "backoff_short"


# ---------------------------------------------------------------------------
# No debe haber detectores duplicados
# ---------------------------------------------------------------------------


def test_no_duplicate_amazon_captcha_detectors_exist():
    """Sólo debe existir UN detector estructural en
    `browser/amazon_captcha_detector.py`. Cualquier substring matching
    en otros archivos debe pasar por el detector central, no detectar
    captcha por su cuenta.
    """
    src = Path(__file__).resolve().parents[3] / "src" / "ofertas_hunter"
    if not src.exists():
        pytest.skip("source tree not found")

    # Patrones que indicarían matching ingenuo de captcha en otros archivos.
    naive_patterns = [
        re.compile(r'"validateCaptcha"\s*in\s+\w+\.lower\(\)', re.IGNORECASE),
        re.compile(r'"captcha"\s*in\s+\w+\.lower\(\)', re.IGNORECASE),
        re.compile(r'"Robot Check"\s*in\s+\w+', re.IGNORECASE),
        re.compile(r'"captchacharacters"\s*in\s+\w+', re.IGNORECASE),
    ]

    offenders: list[tuple[Path, str]] = []
    for py in src.rglob("*.py"):
        # El detector central es el único que puede tener estos patterns.
        if py.name == "amazon_captcha_detector.py":
            continue
        if py.name == "amazon_captcha_audit.py":
            continue  # audit usa el detector — su lógica auxiliar es OK
        text = py.read_text(encoding="utf-8", errors="ignore")
        for pat in naive_patterns:
            for m in pat.finditer(text):
                offenders.append((py.relative_to(src), m.group(0)))
    assert not offenders, (
        "Detección naïve de captcha encontrada fuera del detector central:\n"
        + "\n".join(f"  {p}: {m}" for p, m in offenders)
    )


# ---------------------------------------------------------------------------
# Las muestras del legacy AmazonScrapperIA NO deben confundirse
# ---------------------------------------------------------------------------


def test_amazon_legacy_scrapperia_fixture_not_marked_as_captcha():
    """`AmazonScrapperIA/data/debug_product.html` (página real de producto)
    NO debe marcarse como captcha por el detector central.
    """
    debug_path = (
        Path(__file__).resolve().parents[4]
        / "AmazonScrapperIA" / "data" / "debug_product.html"
    )
    if not debug_path.exists():
        pytest.skip("legacy debug_product.html no disponible")
    html = debug_path.read_text(encoding="utf-8", errors="ignore")
    a = AmazonCaptchaDetector().assess(html=html, status=200)
    assert a.should_pause_marketplace is False


def test_amazon_legacy_scrapperia_search_fixture_not_marked_as_captcha():
    debug_path = (
        Path(__file__).resolve().parents[4]
        / "AmazonScrapperIA" / "data" / "debug_search.html"
    )
    if not debug_path.exists():
        pytest.skip("legacy debug_search.html no disponible")
    html = debug_path.read_text(encoding="utf-8", errors="ignore")
    a = AmazonCaptchaDetector().assess(html=html, status=200)
    assert a.should_pause_marketplace is False


# ---------------------------------------------------------------------------
# CLI: amazon-captcha-check reporta should_pause_marketplace=False
# ---------------------------------------------------------------------------


def test_amazon_captcha_check_cli_reports_should_pause_false_for_medium():
    """La función underlying del CLI (`assess`) reporta should_pause=False
    cuando sólo hay form_action sin texto visible.
    """
    html = (
        '<html><head><title>Producto X</title></head>'
        '<body><div hidden><form action="/errors/validateCaptcha"></form></div>'
        "</body></html>"
    )
    a = AmazonCaptchaDetector().assess(html=html, final_url="https://www.amazon.com.mx/dp/B0X", status=200)
    assert a.should_pause_marketplace is False
    assert a.confidence in ("medium", "low")


# ---------------------------------------------------------------------------
# Launcher option 3 → MCP → AmazonHunterAgent → AmazonCaptchaDetector
# ---------------------------------------------------------------------------


def test_launcher_option3_uses_central_amazon_captcha_detector():
    """El MCP context construye el browser Amazon con `user_data_dir` y
    la detección estructural se hace en `AmazonCaptchaDetector` central.
    Verificamos que la cadena de imports usa el detector central.
    """
    from ofertas_hunter.browser import playwright_worker

    # `playwright_worker` importa el detector central.
    assert hasattr(playwright_worker, "AmazonCaptchaDetector"), (
        "playwright_worker debe usar AmazonCaptchaDetector central"
    )

    # `mcp.context.ServerContext.get_amazon_browser` debe existir.
    from ofertas_hunter.mcp.context import ServerContext

    assert hasattr(ServerContext, "get_amazon_browser"), (
        "ServerContext.get_amazon_browser debe existir para que el MCP "
        "use el browser Amazon dedicado con user_data_dir."
    )
