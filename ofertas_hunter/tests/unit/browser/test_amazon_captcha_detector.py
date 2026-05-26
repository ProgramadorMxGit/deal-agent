"""Tests del detector estructural de captcha de Amazon.

Cubre:

- captcha real (5 escenarios): título "Robot Check", form action validateCaptcha,
  input captchacharacters, texto visible "Continuar a Compras", URL con
  /errors/validateCaptcha.
- falsos positivos (4): substring en script, en JSON telemetry, en
  comentarios, página de producto real con la palabra "captcha" dentro de
  un script.
- HTTP 503 sin texto visible no basta.
- precios faltantes / selectores rotos / timeouts no son captcha.
- pausa: no incrementa por low confidence; sí cuando hay validateCaptcha
  URL + form.
- compatibilidad con muestras del legacy AmazonScrapperIA (HTMLs reales).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ofertas_hunter.browser.amazon_captcha_detector import AmazonCaptchaDetector


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "amazon"
LEGACY_SAMPLES = (
    Path(__file__).resolve().parents[4] / "AmazonScrapperIA" / "data" / "heal_samples"
)


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Real captcha
# ---------------------------------------------------------------------------


class TestCaptchaReal:
    def test_amazon_captcha_real_robot_check_title(self):
        html = _load("real_captcha_robot_check_title.html")
        result = AmazonCaptchaDetector().assess(
            html=html, final_url="https://www.amazon.com.mx/dp/B0CEXAMPLE", status=200
        )
        assert result.is_captcha is True
        assert result.confidence == "high"
        assert "form_action_validate_captcha" in result.strong_signals
        assert "input_captchacharacters" in result.strong_signals
        assert "title_robot_check" in result.visible_signals
        assert result.should_pause_marketplace is True

    def test_amazon_captcha_real_validate_captcha_form(self):
        html = _load("real_captcha_continuar_comprando.html")
        result = AmazonCaptchaDetector().assess(
            html=html,
            final_url="https://www.amazon.com.mx/dp/B0EXAMPLE",
            status=200,
        )
        assert result.is_captcha is True
        assert result.confidence == "high"
        assert "form_action_validate_captcha" in result.strong_signals
        assert any(
            v in result.visible_signals
            for v in ("text_continuar_comprando", "text_continue_shopping")
        )
        assert result.should_pause_marketplace is True

    def test_amazon_captcha_real_captchacharacters_input(self):
        html = (
            '<html><head><title>Robot Check</title></head><body>'
            '<form action="/errors/validateCaptcha">'
            '<input id="captchacharacters" name="field-keywords">'
            "</form></body></html>"
        )
        result = AmazonCaptchaDetector().assess(html=html)
        assert result.confidence == "high"
        assert "input_captchacharacters" in result.strong_signals
        assert "title_robot_check" in result.visible_signals

    def test_amazon_captcha_real_visible_robot_text(self):
        html = (
            '<html><head><title>Amazon.com.mx</title></head><body>'
            '<p>Lo sentimos, parece que está utilizando un programa automatizado</p>'
            '<form action="/errors/validateCaptcha">'
            '<input name="amzn-captcha-token">'
            "</form></body></html>"
        )
        result = AmazonCaptchaDetector().assess(html=html)
        assert result.is_captcha is True
        assert result.confidence == "high"
        assert "form_action_validate_captcha" in result.strong_signals
        assert "input_amzn_captcha" in result.strong_signals
        assert "text_apology_es" in result.visible_signals

    def test_amazon_captcha_real_url_validatecaptcha(self):
        # Sólo URL (sin estructura HTML rica) ya es señal fuerte.
        html = "<html><body>...</body></html>"
        result = AmazonCaptchaDetector().assess(
            html=html,
            final_url=(
                "https://www.amazon.com.mx/errors/validateCaptcha?ie=UTF8"
            ),
            status=200,
        )
        assert "url_validate_captcha" in result.strong_signals
        # Sin texto visible, es medium.
        assert result.confidence == "medium"
        # Sin visible NO debe pausar.
        assert result.should_pause_marketplace is False


# ---------------------------------------------------------------------------
# Falsos positivos
# ---------------------------------------------------------------------------


class TestCaptchaFalsePositive:
    def test_amazon_captcha_false_positive_script_contains_captcha(self):
        html = _load("false_positive_script_validatecaptcha.html")
        result = AmazonCaptchaDetector().assess(html=html)
        assert result.confidence in ("low", "none")
        assert result.is_captcha is False or result.confidence == "low"
        # NO debe pausar marketplace.
        assert result.should_pause_marketplace is False
        # Sin estructura, las señales fuertes deben estar vacías.
        assert result.strong_signals == ()

    def test_amazon_captcha_false_positive_json_contains_captcha(self):
        html = _load("false_positive_json_telemetry.html")
        result = AmazonCaptchaDetector().assess(html=html)
        assert result.should_pause_marketplace is False
        assert result.strong_signals == ()

    def test_amazon_captcha_false_positive_telemetry_contains_captcha(self):
        html = (
            '<html><head><title>Producto X - Amazon.com.mx</title></head><body>'
            '<h1 id="productTitle">Producto X</h1>'
            '<script type="application/json">'
            '{"endpoint":"/errors/validateCaptcha","csm":"amzn-captcha"}'
            "</script></body></html>"
        )
        result = AmazonCaptchaDetector().assess(html=html)
        assert result.should_pause_marketplace is False
        assert result.strong_signals == ()

    def test_amazon_captcha_false_positive_product_page_with_captcha_word_in_script(self):
        html = (
            '<html><head><title>Apple iPhone 16 - Amazon.com.mx</title></head><body>'
            '<h1 id="productTitle">Apple iPhone 16 Pro Max</h1>'
            '<span id="savingsPercentage">-50%</span>'
            '<script>// captcha helper: validateCaptcha endpoint</script>'
            "</body></html>"
        )
        result = AmazonCaptchaDetector().assess(html=html)
        assert result.should_pause_marketplace is False
        # Aunque substring `validateCaptcha` aparezca, NO es captcha real.
        assert result.confidence in ("low", "none")

    def test_amazon_captcha_status_503_without_visible_robot_check_is_not_enough(self):
        html = (
            "<html><head><title>503 Service Unavailable</title></head>"
            "<body><h1>503</h1><p>El servicio no está disponible.</p></body></html>"
        )
        result = AmazonCaptchaDetector().assess(html=html, status=503)
        assert result.should_pause_marketplace is False
        # 503 puro no es captcha.
        assert result.is_captcha is False or result.confidence == "low"

    def test_amazon_missing_price_is_not_captcha(self):
        html = _load("product_minimal_no_price.html")
        result = AmazonCaptchaDetector().assess(html=html)
        assert result.is_captcha is False
        assert result.should_pause_marketplace is False

    def test_amazon_selector_miss_is_not_captcha(self):
        # Página con selectores diferentes a los esperados pero sin captcha.
        html = (
            '<html><head><title>Producto random</title></head><body>'
            '<h2>Producto Random</h2>'
            '<div class="custom-price">$199</div>'
            "</body></html>"
        )
        result = AmazonCaptchaDetector().assess(html=html)
        assert result.is_captcha is False
        assert result.should_pause_marketplace is False

    def test_amazon_timeout_is_not_captcha(self):
        # html vacío simula timeout.
        result = AmazonCaptchaDetector().assess(
            html="", final_url="https://www.amazon.com.mx/dp/B0X", status=0
        )
        assert result.is_captcha is False
        assert result.should_pause_marketplace is False


# ---------------------------------------------------------------------------
# Pausa de marketplace
# ---------------------------------------------------------------------------


class TestCaptchaPauseLogic:
    def test_amazon_does_not_pause_on_low_confidence_captcha(self):
        # Substring en script, sin estructura ni visible.
        html = "<html><body><script>var x = 'validateCaptcha';</script></body></html>"
        result = AmazonCaptchaDetector().assess(html=html)
        assert result.confidence == "low"
        assert result.should_pause_marketplace is False

    def test_amazon_does_not_increment_captcha_counter_on_false_positive(self):
        # Tokens débiles + visibles texto pero sin estructura → medium.
        html = (
            "<html><body><script>amzn-captcha</script>"
            "<p>Continue shopping</p></body></html>"
        )
        result = AmazonCaptchaDetector().assess(html=html)
        assert result.should_pause_marketplace is False

    def test_amazon_pauses_on_repeated_high_confidence_real_captcha(self):
        html = _load("real_captcha_continuar_comprando.html")
        result = AmazonCaptchaDetector().assess(html=html)
        assert result.should_pause_marketplace is True

    def test_amazon_pauses_on_validatecaptcha_url_and_form(self):
        html = _load("real_captcha_continuar_comprando.html")
        result = AmazonCaptchaDetector().assess(
            html=html,
            final_url="https://www.amazon.com.mx/errors/validateCaptcha",
        )
        assert "form_action_validate_captcha" in result.strong_signals
        assert "url_validate_captcha" in result.strong_signals
        assert result.should_pause_marketplace is True


# ---------------------------------------------------------------------------
# Integración con muestras del legacy AmazonScrapperIA
# ---------------------------------------------------------------------------


class TestLegacyIntegration:
    def test_amazon_legacy_real_captcha_samples_classified_high(self):
        """De las 90 muestras de captcha que el legacy guardó, todas deben
        clasificarse como confidence=high.
        """
        if not LEGACY_SAMPLES.exists():
            pytest.skip("AmazonScrapperIA samples no disponibles")

        detector = AmazonCaptchaDetector()
        samples = list(LEGACY_SAMPLES.glob("*.html"))
        if not samples:
            pytest.skip("No hay heal_samples")

        captcha_samples = [
            s for s in samples
            if "validateCaptcha" in s.read_text(encoding="utf-8", errors="ignore")
        ]
        assert len(captcha_samples) > 50, "se esperaban >50 muestras de captcha"

        false_negatives = []
        for s in captcha_samples:
            html = s.read_text(encoding="utf-8", errors="ignore")
            r = detector.assess(html=html)
            if r.confidence != "high":
                false_negatives.append((s.name, r.confidence))
        assert not false_negatives, f"Captcha samples no clasificados como high: {false_negatives[:5]}"

    def test_amazon_legacy_non_captcha_samples_not_marked(self):
        """De las muestras del legacy que NO son captcha, ninguna debe
        marcarse should_pause_marketplace=True.
        """
        if not LEGACY_SAMPLES.exists():
            pytest.skip("AmazonScrapperIA samples no disponibles")

        detector = AmazonCaptchaDetector()
        samples = list(LEGACY_SAMPLES.glob("*.html"))
        non_captcha = [
            s for s in samples
            if "validateCaptcha" not in s.read_text(encoding="utf-8", errors="ignore")
        ]
        if not non_captcha:
            pytest.skip("No hay muestras no-captcha en heal_samples")

        for s in non_captcha:
            html = s.read_text(encoding="utf-8", errors="ignore")
            r = detector.assess(html=html)
            assert r.should_pause_marketplace is False, (
                f"{s.name}: should_pause_marketplace=True (false positive). "
                f"confidence={r.confidence} strong={r.strong_signals}"
            )

    def test_amazon_legacy_like_product_html_not_marked_captcha(self):
        """Una página de producto real con tokens en scripts no se marca."""
        html = _load("false_positive_script_validatecaptcha.html")
        result = AmazonCaptchaDetector().assess(html=html)
        assert result.should_pause_marketplace is False

    def test_amazon_legacy_debug_product_not_marked_captcha(self):
        """Si existe `debug_product.html` en el legacy, no debe marcarse."""
        debug_path = (
            Path(__file__).resolve().parents[4]
            / "AmazonScrapperIA" / "data" / "debug_product.html"
        )
        if not debug_path.exists():
            pytest.skip("debug_product.html no existe en legacy")
        html = debug_path.read_text(encoding="utf-8", errors="ignore")
        result = AmazonCaptchaDetector().assess(html=html)
        assert result.should_pause_marketplace is False

    def test_amazon_legacy_debug_search_not_marked_captcha(self):
        debug_path = (
            Path(__file__).resolve().parents[4]
            / "AmazonScrapperIA" / "data" / "debug_search.html"
        )
        if not debug_path.exists():
            pytest.skip("debug_search.html no existe en legacy")
        html = debug_path.read_text(encoding="utf-8", errors="ignore")
        result = AmazonCaptchaDetector().assess(html=html)
        assert result.should_pause_marketplace is False
