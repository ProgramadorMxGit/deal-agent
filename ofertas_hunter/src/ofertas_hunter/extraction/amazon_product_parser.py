"""Parser de páginas de producto de Amazon México.

Recibe HTML como string y devuelve un `ExtractedProduct`. **Puro**: no toca
red, no usa Playwright. Esto permite tests deterministas con fixtures.

Estrategia (en orden de prioridad):

1. **Selectores CSS clásicos** (`#productTitle`, `#corePrice_*`, `.a-price`,
   `#savingsPercentage`, `#availability`, `#landingImage`).
2. **JSON-LD** embebido en `<script type="application/ld+json">` con
   `@type=Product`.
3. **Open Graph** meta tags (`og:image`, `og:title`, `og:price:amount`).
4. **Twitter Card** meta tags como secundario.

Si los selectores fallan, registra `extraction_warnings` con razones precisas.

Defensas duras:

- **Mensualidad**: si el texto del precio contiene "/mes", "MSI", "pago",
  "mensualidades", "desde", marca `is_monthly_payment=True` y mete razón
  `monthly_payment` en `not_publishable_reasons`.
- **Variant mismatch**: si el listener pasó `expected_title` y el título
  extraído es muy distinto (Jaccard < 0.3 sobre tokens significativos), pone
  el flag y razón. El parser por sí solo no lo aplica salvo que se le pase
  el expected_title vía `validate_against`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from ..marketplaces.base import ExtractedProduct
from ..marketplaces.url_utils import canonicalize_amazon_url, extract_asin
from .price_parser import (
    calculate_discount,
    detect_monthly_payment,
    extract_discount_percent,
    parse_price_text,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Selectores
# ---------------------------------------------------------------------------


_TITLE_SELECTORS = [
    "#productTitle",
    "#title span",
    "#title",
    "h1#title",
    "h1 span",
]

_CURRENT_PRICE_SELECTORS = [
    "#priceblock_dealprice",
    "#priceblock_ourprice",
    "#corePrice_desktop .a-price .a-offscreen",
    "#corePrice_feature_div .a-price .a-offscreen",
    "#apex_offerDisplay_desktop .a-price .a-offscreen",
    "#apex_desktop .a-price .a-offscreen",
    ".priceToPay .a-offscreen",
    "#price_inside_buybox",
    "#newBuyBoxPrice",
    ".a-price[data-a-color='price'] .a-offscreen",
    "#price .a-offscreen",
]

_PREVIOUS_PRICE_SELECTORS = [
    "#priceblock_was_price .a-offscreen",
    ".basisPrice .a-offscreen",
    "#corePrice_desktop .basisPrice .a-offscreen",
    "#apex_offerDisplay_desktop .basisPrice .a-offscreen",
    ".a-price.a-text-price .a-offscreen",
    "[data-a-strike='true'] .a-offscreen",
    "span.a-price.a-text-price[data-a-strike='true'] .a-offscreen",
    ".a-text-strike",
    "#listPrice",
]

_DISCOUNT_BADGE_SELECTORS = [
    "#savingsPercentage",
    ".savingsPercentage",
    "#dealprice_savings .a-color-price",
    ".reinventPriceSavingsPercentageMargin",
    "[id*='savingsPercentage']",
    ".a-badge-text",
]

_AVAILABILITY_SELECTORS = [
    "#availability span",
    "#outOfStock",
    "#availabilityInsideBuyBox_feature_div",
    ".a-color-success",
    ".a-color-state",
    "#availability",
]

_IMAGE_SELECTORS = [
    "#landingImage",
    "#imgBlkFront",
    "#main-image",
    "#imageBlock_feature_div img",
]

_SELLER_SELECTORS = [
    "#sellerProfileTriggerId",
    "#merchant-info",
    "#tabular-buybox .tabular-buybox-text-message",
]

_BREADCRUMB_SELECTORS = [
    "#wayfinding-breadcrumbs_feature_div a",
    "#wayfinding-breadcrumbs_container a",
]

_VARIANT_SELECTORS = [
    "#variation_color_name .selection",
    "#variation_size_name .selection",
    "#twister .a-row.selected",
    ".a-color-base.swatch-title-text-display",
]


_OUT_OF_STOCK_TOKENS = (
    "no disponible",
    "out of stock",
    "sin existencias",
    "currently unavailable",
    "actualmente no disponible",
    "agotado",
)


# ---------------------------------------------------------------------------
# Parser principal
# ---------------------------------------------------------------------------


class AmazonProductParser:
    """Parser determinista de páginas de producto Amazon."""

    def parse(
        self,
        html: str,
        url: str,
        *,
        expected_title: Optional[str] = None,
    ) -> ExtractedProduct:
        soup = BeautifulSoup(html or "", "html.parser")
        canonical = canonicalize_amazon_url(url)
        asin = extract_asin(url)

        product = ExtractedProduct(
            url=url,
            canonical_url=canonical,
            marketplace="amazon",
            asin=asin,
        )

        # --- Title ---
        product.title = self._extract_title(soup) or self._title_from_jsonld(soup)
        if not product.title:
            product.title = self._title_from_meta(soup)
            if product.title:
                product.extraction_warnings.append("title_from_meta")

        # --- Prices ---
        current_text, current_price = self._extract_current_price(soup)
        product.raw_price_text = current_text
        product.current_price = current_price

        # Mensualidad: si el texto contiene mensualidad pero NO hay precio total
        # cerca, marcamos como sospechoso. Si se encuentra ambos (precio total
        # + mensualidad), igualmente marcamos para que el revalidator decida.
        if current_text and detect_monthly_payment(current_text):
            product.is_monthly_payment = True
            product.extraction_warnings.append("price_text_has_monthly_pattern")

        # Validar contra cuerpo principal: si la zona de precio menciona
        # "Desde" antes del número, también es señal de mensualidad.
        if self._has_monthly_payment_zone(soup):
            product.is_monthly_payment = True
            if "monthly_zone_detected" not in product.extraction_warnings:
                product.extraction_warnings.append("monthly_zone_detected")

        previous_text, previous_price = self._extract_previous_price(soup)
        product.raw_previous_price_text = previous_text
        product.previous_price = previous_price

        # --- Discount badge ---
        product.discount_percent = self._extract_discount_badge(soup)

        # --- Calculated discount ---
        if product.current_price and product.previous_price:
            product.calculated_discount_percent = calculate_discount(
                product.current_price, product.previous_price
            )
            # Si no hay badge pero sí cálculo, usamos cálculo.
            if product.discount_percent is None:
                product.discount_percent = product.calculated_discount_percent

        # --- Image ---
        product.image_url = self._extract_image(soup)
        if not product.image_url:
            product.image_url = self._image_from_meta(soup)
            if product.image_url:
                product.extraction_warnings.append("image_from_og")
        if not product.image_url:
            product.image_url = self._image_from_jsonld(soup)
            if product.image_url:
                product.extraction_warnings.append("image_from_json_ld")

        # --- Availability / stock ---
        availability = self._extract_availability(soup)
        product.availability = availability
        product.in_stock = self._infer_stock(soup, availability)

        # --- Seller ---
        product.seller = self._extract_text(soup, _SELLER_SELECTORS)

        # --- Brand ---
        product.brand_guess = self._extract_brand(soup, product.title)

        # --- Category ---
        product.category_guess = self._extract_breadcrumb_last(soup)

        # --- Variant signals ---
        product.selected_variant_signals = self._extract_variants(soup)

        # --- Confianza ---
        product.extraction_confidence = self._compute_confidence(product)

        # --- Validación contra título esperado (variant mismatch) ---
        if expected_title and product.title:
            if not _titles_match(expected_title, product.title):
                product.not_publishable_reasons.append("variant_mismatch")
                product.extraction_warnings.append("title_mismatch_with_expected")

        # --- Gates duros: is_publishable ---
        product.not_publishable_reasons.extend(self._not_publishable_reasons(product))
        product.is_publishable = (
            len(product.not_publishable_reasons) == 0
            and product.title is not None
            and product.image_url is not None
            and product.current_price is not None
        )

        return product

    # ------------------------------------------------------------------
    # Helpers de selectores
    # ------------------------------------------------------------------

    def _extract_title(self, soup: BeautifulSoup) -> Optional[str]:
        return self._extract_text(soup, _TITLE_SELECTORS)

    def _extract_text(self, soup: BeautifulSoup, selectors: list[str]) -> Optional[str]:
        for sel in selectors:
            try:
                el = soup.select_one(sel)
            except Exception:
                continue
            if el:
                text = el.get_text(strip=True)
                if text:
                    return text
        return None

    def _extract_current_price(
        self, soup: BeautifulSoup
    ) -> tuple[Optional[str], Optional[float]]:
        for sel in _CURRENT_PRICE_SELECTORS:
            for el in soup.select(sel):
                text = el.get_text(strip=True)
                if not text:
                    continue
                # Si claramente es mensualidad solo, saltar (pero registrar).
                if detect_monthly_payment(text) and "$" not in text:
                    continue
                value = parse_price_text(text)
                if value and value > 0:
                    return text, value
        # Fallback JSON-LD price
        ld_price = self._price_from_jsonld(soup)
        if ld_price is not None:
            return f"{ld_price}", ld_price
        # Fallback OG
        og = soup.select_one('meta[property="og:price:amount"]')
        if og and og.get("content"):
            value = parse_price_text(og["content"])
            if value:
                return og["content"], value
        return None, None

    def _extract_previous_price(
        self, soup: BeautifulSoup
    ) -> tuple[Optional[str], Optional[float]]:
        for sel in _PREVIOUS_PRICE_SELECTORS:
            for el in soup.select(sel):
                text = el.get_text(strip=True)
                if not text:
                    continue
                value = parse_price_text(text)
                if value and value > 0:
                    return text, value
        return None, None

    def _extract_discount_badge(self, soup: BeautifulSoup) -> Optional[float]:
        for sel in _DISCOUNT_BADGE_SELECTORS:
            for el in soup.select(sel):
                text = el.get_text(strip=True)
                pct = extract_discount_percent(text)
                if pct and pct >= 5:
                    return float(pct)
        return None

    def _extract_image(self, soup: BeautifulSoup) -> Optional[str]:
        for sel in _IMAGE_SELECTORS:
            el = soup.select_one(sel)
            if not el:
                continue
            for attr in ("data-old-hires", "data-a-dynamic-image", "src"):
                value = el.get(attr)
                if not value:
                    continue
                if attr == "data-a-dynamic-image":
                    # JSON con varias URLs
                    try:
                        data = json.loads(value)
                        if isinstance(data, dict) and data:
                            return next(iter(data.keys()))
                    except json.JSONDecodeError:
                        continue
                if isinstance(value, str) and value.startswith("http"):
                    return value
        return None

    def _extract_availability(self, soup: BeautifulSoup) -> Optional[str]:
        for sel in _AVAILABILITY_SELECTORS:
            el = soup.select_one(sel)
            if not el:
                continue
            text = el.get_text(strip=True)
            if text:
                return text
        return None

    def _infer_stock(
        self, soup: BeautifulSoup, availability_text: Optional[str]
    ) -> Optional[bool]:
        if availability_text:
            lower = availability_text.lower()
            if any(token in lower for token in _OUT_OF_STOCK_TOKENS):
                return False
            if "en stock" in lower or "in stock" in lower or "disponible" in lower:
                return True
        # Heurística adicional
        out = soup.select_one("#outOfStock")
        if out:
            return False
        atc = soup.select_one("#add-to-cart-button, #buy-now-button")
        if atc:
            return True
        return None

    def _extract_brand(
        self, soup: BeautifulSoup, title: Optional[str]
    ) -> Optional[str]:
        # Ficha técnica "Marca"
        for el in soup.select("tr.po-brand .a-span9 span, #bylineInfo"):
            text = el.get_text(strip=True)
            text = text.replace("Visita la Tienda de", "").replace("Marca:", "").strip()
            if text and len(text) <= 40:
                return text.split()[0].lower()
        if not title:
            return None
        first = title.split()[0]
        return first.lower() if len(first) >= 2 else None

    def _extract_breadcrumb_last(self, soup: BeautifulSoup) -> Optional[str]:
        for sel in _BREADCRUMB_SELECTORS:
            crumbs = soup.select(sel)
            if crumbs:
                last = crumbs[-1].get_text(strip=True)
                if last:
                    return last.lower()
        return None

    def _extract_variants(self, soup: BeautifulSoup) -> dict:
        signals: dict[str, str] = {}
        for sel in _VARIANT_SELECTORS:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(strip=True)
                if text:
                    key = sel.split("_")[1] if "_" in sel else sel
                    signals[key.replace(" ", "_")] = text
        return signals

    # ------------------------------------------------------------------
    # Fallbacks: meta + json-ld
    # ------------------------------------------------------------------

    def _title_from_meta(self, soup: BeautifulSoup) -> Optional[str]:
        for sel in ('meta[property="og:title"]', 'meta[name="title"]'):
            el = soup.select_one(sel)
            if el and el.get("content"):
                return el["content"].strip()
        return None

    def _image_from_meta(self, soup: BeautifulSoup) -> Optional[str]:
        for sel in ('meta[property="og:image"]', 'meta[name="twitter:image"]'):
            el = soup.select_one(sel)
            if el and el.get("content"):
                return el["content"].strip()
        return None

    def _price_from_jsonld(self, soup: BeautifulSoup) -> Optional[float]:
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            for entry in _iter_ld_entries(data):
                offers = entry.get("offers")
                if not offers:
                    continue
                if isinstance(offers, list):
                    for offer in offers:
                        price = offer.get("price")
                        if price is not None:
                            try:
                                return float(price)
                            except (TypeError, ValueError):
                                continue
                elif isinstance(offers, dict):
                    price = offers.get("price")
                    if price is not None:
                        try:
                            return float(price)
                        except (TypeError, ValueError):
                            continue
        return None

    def _title_from_jsonld(self, soup: BeautifulSoup) -> Optional[str]:
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            for entry in _iter_ld_entries(data):
                if entry.get("@type") == "Product":
                    name = entry.get("name")
                    if name:
                        return str(name).strip()
        return None

    def _image_from_jsonld(self, soup: BeautifulSoup) -> Optional[str]:
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            for entry in _iter_ld_entries(data):
                image = entry.get("image")
                if isinstance(image, list) and image:
                    return image[0]
                if isinstance(image, str):
                    return image
        return None

    def _has_monthly_payment_zone(self, soup: BeautifulSoup) -> bool:
        for sel in (
            "#installmentCalculator_feature_div",
            "#monthlyPayment_feature_div",
            "#corePrice_feature_div",
        ):
            el = soup.select_one(sel)
            if el and detect_monthly_payment(el.get_text(" ", strip=True)):
                return True
        return False

    # ------------------------------------------------------------------
    # Confianza y gates
    # ------------------------------------------------------------------

    def _compute_confidence(self, product: ExtractedProduct) -> str:
        score = 0
        if product.title:
            score += 1
        if product.current_price:
            score += 1
        if product.previous_price:
            score += 1
        if product.image_url:
            score += 1
        if product.discount_percent:
            score += 1
        if product.in_stock is True:
            score += 1
        if product.is_monthly_payment:
            score -= 2
        if score >= 5:
            return "high"
        if score >= 3:
            return "medium"
        return "low"

    def _not_publishable_reasons(self, product: ExtractedProduct) -> list[str]:
        reasons: list[str] = []
        if product.title is None:
            reasons.append("no_title")
        if product.image_url is None:
            reasons.append("no_image")
        if product.current_price is None:
            reasons.append("no_price")
        if product.is_monthly_payment:
            reasons.append("monthly_payment")
        if product.in_stock is False:
            reasons.append("out_of_stock")
        return reasons


# ---------------------------------------------------------------------------
# Helpers libres
# ---------------------------------------------------------------------------


def _iter_ld_entries(data):
    """Itera entradas JSON-LD soportando dict, list y `@graph`."""
    if isinstance(data, list):
        for item in data:
            yield from _iter_ld_entries(item)
    elif isinstance(data, dict):
        if "@graph" in data and isinstance(data["@graph"], list):
            yield from _iter_ld_entries(data["@graph"])
        else:
            yield data


_STOPWORDS = {
    "el", "la", "los", "las", "de", "del", "y", "o", "u", "con", "para",
    "por", "en", "un", "una", "unos", "unas", "al", "a", "the", "and",
    "for", "with", "of", "in",
}


def _tokenize(text: str) -> set[str]:
    tokens = re.findall(r"[A-Za-z0-9]+", text.lower())
    return {t for t in tokens if t not in _STOPWORDS and len(t) > 1}


def _titles_match(expected: str, found: str, threshold: float = 0.3) -> bool:
    """Devuelve True si los títulos parecen ser el mismo producto.

    Usa una mezcla de:
    - **inclusión**: si la mayoría (>=70%) de tokens significativos del
      `expected` están en el `found`, los títulos coinciden (caso típico:
      mensaje Telegram corto vs título completo de Amazon).
    - **Jaccard**: similitud simétrica como fallback.
    """
    e = _tokenize(expected)
    f = _tokenize(found)
    if not e or not f:
        return False
    inter = e & f
    coverage = len(inter) / len(e)
    if coverage >= 0.7:
        return True
    union = len(e | f) or 1
    return (len(inter) / union) >= threshold
