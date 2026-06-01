"""Tests de extract_verified_ml_prices (FASE 7, criterios A-J).

Fixtures HTML inline que replican la estructura real de ML (andes-money-amount).
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from ofertas_hunter.marketplaces.ml_price_verification import (
    extract_verified_ml_prices, detect_unit_price,
)


def _soup(html):
    return BeautifulSoup(html, "html.parser")


def _amount(value_int, cents="00", extra_class=""):
    return (
        f'<span class="andes-money-amount {extra_class}">'
        f'<span class="andes-money-amount__currency-symbol">$</span>'
        f'<span class="andes-money-amount__fraction">{value_int}</span>'
        f'<span class="andes-money-amount__cents">{cents}</span>'
        f'</span>'
    )


# A — previous tachado real visible -> verificado, descuento calculado
def test_A_real_previous_verified():
    html = f'''
    <div class="ui-pdp-price__main-container">
      <s class="ui-pdp-price__original-value">{_amount("1,300")}</s>
      <div class="ui-pdp-price__second-line">{_amount("650")}</div>
      <span class="andes-money-amount__discount">50% OFF</span>
    </div>'''
    r = extract_verified_ml_prices(_soup(html))
    assert r["current_price"] == 650.0
    assert r["previous_price"] == 1300.0
    assert r["current_price_verified"] is True
    assert r["ml_previous_price_verified"] is True
    assert r["discount_percent_verified"] is True
    assert round(r["discount_percent"]) == 50
    assert r["previous_price_selector"] == "ui-pdp-price__original-value"


# B — sin previous tachado -> NO verificado, NO inventa
def test_B_no_previous_not_verified():
    html = f'''
    <div class="ui-pdp-price__main-container">
      <div class="ui-pdp-price__second-line">{_amount("650")}</div>
      <span class="andes-money-amount__discount">78% OFF</span>
    </div>'''
    r = extract_verified_ml_prices(_soup(html))
    assert r["current_price"] == 650.0
    assert r["previous_price"] is None
    assert r["ml_previous_price_verified"] is False
    assert r["discount_percent_verified"] is False
    # el badge existe pero NO se verifica ni se usa para inventar previous
    assert r["discount_percent"] == 78.0
    assert r["discount_percent_source"] == "badge_unverified"


# C — Blu-e replay: current 650, badge 78, sin previous -> no verificado
def test_C_blue_replay_blocked_signal():
    html = f'''
    <div class="ui-pdp-price__second-line">{_amount("650")}</div>
    <span class="andes-money-amount__discount">78% OFF</span>'''
    r = extract_verified_ml_prices(_soup(html))
    assert r["ml_previous_price_verified"] is False
    assert r["previous_price"] is None


# D — precio por kilo en raw -> current_price_is_unit_price
def test_D_unit_price_per_kilo():
    assert detect_unit_price("$650 ($295.45 por kilo)") is True
    assert detect_unit_price("$295.45 /kg") is True
    assert detect_unit_price("$650") is False
    html = f'''
    <div class="ui-pdp-price__second-line">
      <span class="andes-money-amount"><span class="andes-money-amount__fraction">295</span>
      <span class="andes-money-amount__cents">45</span></span> por kilo
    </div>'''
    r = extract_verified_ml_prices(_soup(html))
    assert r["current_price_is_unit_price"] is True
    assert r["current_price_verified"] is False


# E — mensualidad -> current_price_is_installment
def test_E_installment():
    html = '''
    <div class="ui-pdp-price__second-line">
      <span class="andes-money-amount"><span class="andes-money-amount__fraction">162</span>
      <span class="andes-money-amount__cents">50</span></span> en 4 meses sin intereses
    </div>'''
    r = extract_verified_ml_prices(_soup(html))
    assert r["current_price_is_installment"] is True
    assert r["current_price_verified"] is False


# F — variante distinta (title mismatch) -> variant_mismatch
def test_F_variant_mismatch():
    html = f'<div class="ui-pdp-price__second-line">{_amount("650")}</div>'
    r = extract_verified_ml_prices(_soup(html), has_variations=True, title_match=False)
    assert r["ml_variant_mismatch"] is True
    assert r["ml_variant_verified"] is False


# G — variante correcta -> verificada
def test_G_variant_ok():
    html = f'''
    <s class="ui-pdp-price__original-value">{_amount("1,300")}</s>
    <div class="ui-pdp-price__second-line">{_amount("650")}</div>'''
    r = extract_verified_ml_prices(_soup(html), has_variations=True, title_match=True)
    assert r["ml_variant_verified"] is True
    assert r["ml_variant_mismatch"] is False


# H — badge visible pero previous missing -> no permite inventar previous
def test_H_badge_no_previous_no_invention():
    html = f'''
    <div class="ui-pdp-price__second-line">{_amount("650")}</div>
    <span class="andes-money-amount__discount">78% OFF</span>'''
    r = extract_verified_ml_prices(_soup(html))
    assert r["previous_price"] is None
    assert r["ml_previous_price_verified"] is False


# I — previous menor que current (incoherente) -> no verificado
def test_I_previous_lower_than_current_not_verified():
    html = f'''
    <s class="ui-pdp-price__original-value">{_amount("500")}</s>
    <div class="ui-pdp-price__second-line">{_amount("650")}</div>'''
    r = extract_verified_ml_prices(_soup(html))
    # previous (500) < current (650) -> NO verificado
    assert r["ml_previous_price_verified"] is False


# J — previous con andes-money-amount--previous (selector alterno)
def test_J_andes_previous_selector():
    html = f'''
    <div class="ui-pdp-price__second-line">{_amount("650")}</div>
    {_amount("1,300", extra_class="andes-money-amount--previous")}'''
    r = extract_verified_ml_prices(_soup(html))
    assert r["previous_price"] == 1300.0
    assert r["ml_previous_price_verified"] is True
    assert r["previous_price_selector"] == "andes-money-amount--previous"
