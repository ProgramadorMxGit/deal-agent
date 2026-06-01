"""Extracción VERIFICADA de precios de Mercado Libre (anti falsos positivos).

Produce un dict con precio actual/anterior + flags de verificación, fuente,
selector y raw text. Reglas:
- current_price: precio principal visible (ui-pdp-price__second-line / main).
- previous_price: SOLO del bloque tachado real (ui-pdp-price__original-value
  / andes-money-amount--previous). NUNCA se inventa desde el descuento.
- detecta precio por unidad (kilo/gramo/litro/ml/pieza) y mensualidad/cuota.
- variant: registra atributos seleccionados; marca mismatch si el título
  esperado no coincide.
- discount verificado SOLO si previous_price real > current_price.

Es puro (input BeautifulSoup) y testeable con fixtures.
"""
from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup

from ..extraction.price_parser import (
    detect_monthly_payment,
    parse_price_text,
)


# Texto que delata precio por unidad de medida o cuota dentro del raw text.
_UNIT_PRICE_RE = re.compile(
    r"(/|\bpor\b)\s*"
    r"(kilo(?:gramo)?s?|kg|gramos?|gr?\b|litros?|lt?\b|ml\b|mililitros?|"
    r"onzas?|oz\b|unidad(?:es)?|pieza(?:s)?|pza\.?|porci[oó]n(?:es)?|c/u|cada\s+uno)",
    re.IGNORECASE,
)


# Selectores de precio anterior tachado (orden de preferencia).
_PREVIOUS_SELECTORS = [
    ("ui-pdp-price__original-value", "class_contains"),
    ("andes-money-amount--previous", "class_contains"),
]

# Selectores de precio actual.
_CURRENT_CONTAINERS = [
    "ui-pdp-price__second-line",
    "ui-pdp-price__main-container",
    "ui-pdp-price__main-price",
]


def _amount_from_andes_element(el) -> Optional[float]:
    """Combina `__fraction` + `__cents` en un float. (replica del parser)."""
    if el is None:
        return None
    fraction_el = el.find(class_="andes-money-amount__fraction")
    if not fraction_el:
        return None
    fraction_text = re.sub(r"[^\d]", "", fraction_el.get_text())
    if not fraction_text:
        return None
    fraction = int(fraction_text)
    cents_el = el.find(class_=lambda c: c and "andes-money-amount__cents" in c)
    cents = 0
    if cents_el:
        cents_text = re.sub(r"[^\d]", "", cents_el.get_text())
        if cents_text:
            cents = int(cents_text)
    return round(fraction + cents / 100, 2)


def detect_unit_price(text: Optional[str]) -> bool:
    """True si el texto sugiere precio por unidad de medida (kilo/gramo/etc.)."""
    if not text:
        return False
    return bool(_UNIT_PRICE_RE.search(text))


def _extract_current(soup: BeautifulSoup):
    """Devuelve (price, raw_text, selector) del precio actual visible.

    `raw_text` incluye el texto del contenedor (no solo el span del monto) para
    poder detectar 'por kilo' / 'meses sin intereses' que ML coloca al lado.
    """
    second_line = soup.find(class_=lambda c: c and "ui-pdp-price__second-line" in c)
    if second_line:
        for span in second_line.find_all(
            class_=lambda c: c and "andes-money-amount" in c
            and "andes-money-amount__discount" not in c
            and "andes-money-amount--previous" not in c
        ):
            val = _amount_from_andes_element(span)
            if val and val > 0:
                # Texto del contenedor completo (incluye "por kilo" / "meses").
                container_text = second_line.get_text(" ", strip=True)
                return val, container_text, "ui-pdp-price__second-line"
    main_price = soup.find(class_=lambda c: c and "ui-pdp-price__main-price" in c)
    if main_price:
        val = _amount_from_andes_element(main_price)
        if val and val > 0:
            parent = main_price.parent if main_price.parent else main_price
            return val, parent.get_text(" ", strip=True), "ui-pdp-price__main-price"
    return None, None, None


def _extract_previous(soup: BeautifulSoup):
    """Devuelve (price, raw_text, selector) del precio anterior tachado real."""
    for cls, _ in _PREVIOUS_SELECTORS:
        container = soup.find(class_=lambda c, _cls=cls: c and _cls in c)
        if container:
            val = _amount_from_andes_element(container)
            raw = container.get_text(" ", strip=True) or container.get("aria-label", "")
            if val is None:
                label = container.get("aria-label", "")
                m = re.search(r"(\d[\d,\.]*)\s*pesos", label)
                if m:
                    val = parse_price_text(m.group(1))
            if val and val > 0:
                return val, raw, cls
    return None, None, None


def _extract_discount_badge(soup: BeautifulSoup):
    """Devuelve (pct, raw_text) del badge de descuento visible, o (None, None)."""
    for el in soup.find_all(class_=lambda c: c and "andes-money-amount__discount" in c):
        text = el.get_text(strip=True)
        m = re.search(r"(\d{1,3})\s*%", text)
        if m:
            v = int(m.group(1))
            if 1 <= v <= 99:
                return v, text
    return None, None


def extract_verified_ml_prices(
    soup: BeautifulSoup,
    *,
    expected_title: Optional[str] = None,
    selected_attributes: Optional[dict] = None,
    has_variations: bool = False,
    title_match: Optional[bool] = None,
) -> dict:
    """Extrae precios ML con flags de verificación. NUNCA inventa previous_price."""
    cur, cur_raw, cur_sel = _extract_current(soup)
    prev, prev_raw, prev_sel = _extract_previous(soup)
    disc_badge, disc_raw = _extract_discount_badge(soup)

    is_installment = bool(cur_raw and detect_monthly_payment(cur_raw))
    is_unit_price = bool(cur_raw and detect_unit_price(cur_raw)) or bool(
        prev_raw and detect_unit_price(prev_raw)
    )

    current_price_verified = (
        cur is not None and cur > 0 and not is_installment and not is_unit_price
    )

    # previous verificado SOLO si vino del bloque tachado real y es > current.
    previous_price_verified = (
        prev is not None and cur is not None and prev > cur and not is_unit_price
    )

    # discount verificado: calculado desde precios reales verificados.
    discount_percent = None
    discount_verified = False
    discount_source = None
    if previous_price_verified and current_price_verified:
        discount_percent = round((prev - cur) / prev * 100, 2)
        discount_verified = True
        discount_source = "computed_from_verified_prices"
    elif disc_badge is not None:
        # Hay badge pero sin previous real: NO se verifica (no inventar).
        discount_percent = float(disc_badge)
        discount_verified = False
        discount_source = "badge_unverified"

    # variante
    variant_mismatch = (title_match is False)
    variant_verified = (
        (not has_variations) or (title_match is True)
    )

    return {
        "current_price": cur,
        "current_price_verified": current_price_verified,
        "current_price_source": "pdp" if cur is not None else None,
        "current_price_selector": cur_sel,
        "current_price_raw_text": cur_raw,
        "previous_price": prev,
        "ml_previous_price_verified": previous_price_verified,
        "previous_price_source": "pdp_strikethrough" if prev is not None else None,
        "previous_price_selector": prev_sel,
        "previous_price_raw_text": prev_raw,
        "discount_percent": discount_percent,
        "discount_percent_verified": discount_verified,
        "discount_percent_source": discount_source,
        "discount_percent_raw_text": disc_raw,
        "current_price_is_unit_price": is_unit_price,
        "current_price_is_installment": is_installment,
        "selected_attributes": selected_attributes or {},
        "ml_variant_verified": variant_verified,
        "ml_variant_mismatch": variant_mismatch,
    }


__all__ = ["extract_verified_ml_prices", "detect_unit_price"]
