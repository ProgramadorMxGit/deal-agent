"""Tests de los helpers puros del ScreenshotCapturer (sin navegador)."""

from __future__ import annotations

from ofertas_hunter.publishing.screenshot_capturer import (
    normalize_ml_pdp_url,
    resolve_capture_url,
)


def test_normalize_ml_rewrites_articulo_catalog_to_www():
    raw = "https://articulo.mercadolibre.com.mx/dreame-d15/p/MLM63750274"
    fixed = normalize_ml_pdp_url(raw)
    assert fixed == "https://www.mercadolibre.com.mx/dreame-d15/p/MLM63750274"


def test_normalize_ml_leaves_www_untouched():
    url = "https://www.mercadolibre.com.mx/x/p/MLM1"
    assert normalize_ml_pdp_url(url) == url


def test_normalize_ml_leaves_non_catalog_articulo_untouched():
    # Sin /p/ → no es URL de catálogo, no se reescribe.
    url = "https://articulo.mercadolibre.com.mx/MLM-123-titulo"
    assert normalize_ml_pdp_url(url) == url


def test_resolve_capture_url_ml_uses_canonical_not_short_link():
    payload = {
        "marketplace": "mercadolibre",
        "url": "https://meli.la/abc",
        "affiliate_url": "https://meli.la/abc",
        "canonical_url": "https://articulo.mercadolibre.com.mx/x/p/MLM9",
    }
    # Debe usar canonical reescrita a www, NUNCA el short link meli.la.
    assert resolve_capture_url(payload) == "https://www.mercadolibre.com.mx/x/p/MLM9"


def test_resolve_capture_url_ml_skips_meli_la_when_no_canonical():
    payload = {
        "marketplace": "mercadolibre",
        "url": "https://meli.la/abc",
        "affiliate_url": "https://meli.la/abc",
    }
    assert resolve_capture_url(payload) is None


def test_resolve_capture_url_amazon_prefers_canonical():
    payload = {
        "marketplace": "amazon",
        "canonical_url": "https://www.amazon.com.mx/dp/B09GK544PP",
        "url": "https://amzn.to/xyz",
    }
    assert resolve_capture_url(payload) == "https://www.amazon.com.mx/dp/B09GK544PP"


def test_resolve_capture_url_amazon_falls_back_to_url():
    payload = {
        "marketplace": "amazon",
        "url": "https://www.amazon.com.mx/dp/B00861CC7Y?tag=x",
    }
    assert resolve_capture_url(payload) == "https://www.amazon.com.mx/dp/B00861CC7Y?tag=x"


def test_dismiss_overlays_js_targets_coachmark_and_floater():
    """El JS de dismiss debe apuntar a los coachmarks de ML y al react-floater
    que envuelve el popup 'Haz tu primera compra mayorista' (precios por unidad).
    Sin esto, el screenshot capturaba el overlay mayorista y engañaba al lector.
    """
    from ofertas_hunter.publishing.screenshot_capturer import _DISMISS_OVERLAYS_JS

    js = _DISMISS_OVERLAYS_JS
    assert "coach-mark" in js
    assert "__floater" in js
    assert "andes-popper" in js
    # Debe ocultar con display:none !important para ganar a estilos inline.
    assert "display" in js and "none" in js
