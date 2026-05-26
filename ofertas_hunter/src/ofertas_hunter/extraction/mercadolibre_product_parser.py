"""Parser de páginas de producto de Mercado Libre México.

Recibe HTML como string y devuelve un `ExtractedProduct`. Puro: sin red, sin
Playwright, testeable con fixtures.

Estrategia (reutilizando lógica del legacy `bot_diversidad_global` + nuevas
defensas anti falso positivo):

1. **Estructura ML real**:
   - precio actual: `<div class="ui-pdp-price__second-line">` >
     `<span class="andes-money-amount">` (NO `__discount`).
   - precio anterior: elemento con clase `ui-pdp-price__original-value`.
   - Combinación `andes-money-amount__fraction` + `__cents`.
2. **Fallbacks**:
   - JSON-LD `application/ld+json` con `@type=Product`.
   - Open Graph `og:title`, `og:image`.
   - aria-label "X pesos" como último recurso.
3. **Defensas**:
   - mensualidad (`/mes`, "MSI"), variant_mismatch, sin stock, condición
     usada / reacondicionada (con regla "needs extreme discount").

`has_share_button`: refleja la regla del legacy (botón Compartir → afiliado).
No es obligatorio para publicar (el bot puede usar links normales), pero el
parser lo expone vía `selected_variant_signals["share_button"]`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..marketplaces.base import ExtractedProduct
from .price_parser import (
    calculate_discount,
    detect_monthly_payment,
    extract_discount_percent,
    parse_price_text,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Regex y constantes
# ---------------------------------------------------------------------------


_ITEM_ID_RE = re.compile(r"(?:^|/)(MLM\d+)(?=\D|$)", re.IGNORECASE)


_USED_CONDITION_TOKENS = (
    "usado",
    "reacondicionado",
    "refurbished",
    "second hand",
    "segunda mano",
    "open box",
)


_OUT_OF_STOCK_TOKENS = (
    "no disponible",
    "sin stock",
    "agotado",
    "currently unavailable",
    "esta publicación finalizó",
    "out of stock",
)


# ---------------------------------------------------------------------------
# Helpers de URL
# ---------------------------------------------------------------------------


def extract_item_id(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    match = _ITEM_ID_RE.search(url)
    return match.group(1).upper() if match else None


def canonicalize_mercadolibre_url(url: str) -> str:
    """Normaliza URL ML quitando tracking params pero preservando el slug del título.

    ML requiere el slug para que el link funcione:
      ✅ https://articulo.mercadolibre.com.mx/MLM18956615-titulo-del-producto
      ❌ https://articulo.mercadolibre.com.mx/MLM18956615  (no abre)

    Si la URL ya tiene el slug (path con guiones después del ID), lo preserva.
    Si solo tiene el ID, lo deja tal cual (mejor que nada).
    """
    parsed = urlparse(url)
    if not parsed.scheme:
        return url

    item_id = extract_item_id(url)
    if not item_id:
        # Sin item_id: devolvemos sin query/fragment
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        return base

    # Si la URL ya tiene el slug en el path, preservarlo
    # Ejemplo: /MLM18956615-titulo-del-producto → preservar
    path = parsed.path.rstrip("/")
    if path and "-" in path:
        # Tiene slug — usar la URL limpia sin query params
        return f"https://articulo.mercadolibre.com.mx{path}"

    # Solo tiene el ID sin slug — devolver con ID solamente
    # (el slug se perderá pero al menos el ID es correcto)
    return f"https://articulo.mercadolibre.com.mx/{item_id}"


def is_mercadolibre_url(url: Optional[str]) -> bool:
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host.endswith("mercadolibre.com.mx") or host == "meli.la" or host.endswith(".meli.la")


# ---------------------------------------------------------------------------
# Helpers de precio (heredados del legacy y extendidos)
# ---------------------------------------------------------------------------


def _amount_from_andes_element(el) -> Optional[float]:
    """Combina `__fraction` + `__cents` en un float."""
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


# ---------------------------------------------------------------------------
# Funciones libres reutilizables
# ---------------------------------------------------------------------------


def has_share_button(html: str) -> bool:
    """Detecta el botón "Compartir" (afiliado) en la página de producto.

    Lógica heredada del legacy:
    1. `data-testid="generate_link_button"` (primario).
    2. Botón con texto "Compartir" dentro de `.toolbar__actions`.
    3. Cualquier botón con texto "Compartir" (terciario, último recurso).
    """
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")

    btn = soup.find("button", attrs={"data-testid": "generate_link_button"})
    if btn:
        return True

    toolbar = soup.find(class_="toolbar__actions")
    if toolbar:
        for b in toolbar.find_all("button"):
            if "Compartir" in b.get_text():
                return True

    for b in soup.find_all("button"):
        if b.get_text(strip=True) == "Compartir":
            return True

    return False


def extract_title(soup: BeautifulSoup) -> Optional[str]:
    el = soup.find(class_=lambda c: c and "ui-pdp-title" in c)
    if el:
        text = el.get_text(strip=True)
        if text:
            return text
    h1 = soup.find("h1")
    if h1:
        text = h1.get_text(strip=True)
        if text:
            return text
    return None


def extract_prices(soup: BeautifulSoup) -> tuple[Optional[float], Optional[float], Optional[str], Optional[str]]:
    """Devuelve `(current, previous, raw_current_text, raw_previous_text)`."""
    current_price: Optional[float] = None
    previous_price: Optional[float] = None
    raw_current: Optional[str] = None
    raw_previous: Optional[str] = None

    # --- Previous price (S con ui-pdp-price__original-value) ---
    prev_container = soup.find(class_=lambda c: c and "ui-pdp-price__original-value" in c)
    if prev_container:
        previous_price = _amount_from_andes_element(prev_container)
        raw_previous = prev_container.get_text(" ", strip=True) or prev_container.get(
            "aria-label", ""
        )
        if previous_price is None:
            label = prev_container.get("aria-label", "")
            m = re.search(r"(\d[\d,\.]*)\s*pesos", label)
            if m:
                previous_price = parse_price_text(m.group(1))

    # --- Current price ---
    second_line = soup.find(class_=lambda c: c and "ui-pdp-price__second-line" in c)
    if second_line:
        for span in second_line.find_all(
            class_=lambda c: c
            and "andes-money-amount" in c
            and "andes-money-amount__discount" not in c
        ):
            val = _amount_from_andes_element(span)
            if val and val > 0:
                current_price = val
                raw_current = span.get_text(" ", strip=True) or span.get("aria-label", "")
                break

    if current_price is None:
        main_price = soup.find(class_=lambda c: c and "ui-pdp-price__main-price" in c)
        if main_price:
            current_price = _amount_from_andes_element(main_price)
            if current_price is not None:
                raw_current = main_price.get_text(" ", strip=True)

    if current_price is None:
        # Fallback aria-label X pesos (ignorando "Antes:")
        for el in soup.find_all(attrs={"aria-label": True}):
            label = el.get("aria-label", "")
            if "Antes" in label or "antes" in label:
                continue
            m = re.search(r"(\d[\d,\.]*)\s*pesos", label)
            if m:
                val = parse_price_text(m.group(1))
                if val and val > 0:
                    current_price = val
                    raw_current = label
                    break

    return current_price, previous_price, raw_current, raw_previous


# ---------------------------------------------------------------------------
# Parser principal
# ---------------------------------------------------------------------------


class MercadoLibreProductParser:
    """Parser determinista de páginas de producto Mercado Libre."""

    USED_REQUIRES_DISCOUNT_PERCENT: float = 70.0

    def parse(
        self,
        html: str,
        url: str,
        *,
        expected_title: Optional[str] = None,
    ) -> ExtractedProduct:
        soup = BeautifulSoup(html or "", "html.parser")
        canonical = canonicalize_mercadolibre_url(url)
        item_id = extract_item_id(url) or extract_item_id(canonical)

        product = ExtractedProduct(
            url=url,
            canonical_url=canonical,
            marketplace="mercadolibre",
            asin=item_id,
        )

        # --- Detección de página no encontrada / producto dado de baja ---
        if self._is_page_not_found(soup, html):
            product.not_publishable_reasons.append("product_not_found")
            product.is_publishable = False
            product.extraction_warnings.append("page_not_found")
            return product

        # --- Title ---
        product.title = extract_title(soup) or self._title_from_jsonld(soup) or self._title_from_meta(soup)
        if product.title and not extract_title(soup):
            product.extraction_warnings.append("title_from_jsonld_or_meta")

        # --- Prices ---
        current, previous, raw_current, raw_previous = extract_prices(soup)
        product.current_price = current
        product.previous_price = previous
        product.raw_price_text = raw_current
        product.raw_previous_price_text = raw_previous

        # JSON-LD price fallback
        if product.current_price is None:
            ld_price = self._price_from_jsonld(soup)
            if ld_price:
                product.current_price = ld_price
                product.raw_price_text = str(ld_price)
                product.extraction_warnings.append("price_from_jsonld")

        # --- Discount ---
        product.discount_percent = self._extract_discount_visible(soup)
        if product.current_price and product.previous_price:
            calc = calculate_discount(product.current_price, product.previous_price)
            product.calculated_discount_percent = calc
            if product.discount_percent is None and calc:
                product.discount_percent = calc

        # --- Image ---
        product.image_url = (
            self._extract_image(soup)
            or self._image_from_meta(soup)
            or self._image_from_jsonld(soup)
        )
        if product.image_url and not self._extract_image(soup):
            if soup.select_one('meta[property="og:image"]'):
                product.extraction_warnings.append("image_from_og")
            else:
                product.extraction_warnings.append("image_from_json_ld")

        # --- Stock / availability ---
        availability = self._extract_availability(soup)
        product.availability = availability
        product.in_stock = self._infer_stock(soup, availability)

        # --- Seller ---
        product.seller = self._extract_seller(soup)

        # --- Brand ---
        product.brand_guess = self._extract_brand(soup, product.title)

        # --- Category ---
        product.category_guess = self._extract_breadcrumb_last(soup)

        # --- Condition ---
        product.condition = self._infer_condition(soup, product.title)

        # --- Variantes / share button ---
        product.selected_variant_signals = self._extract_variants(soup)
        product.selected_variant_signals["share_button"] = has_share_button(html)

        # --- Mensualidad ---
        if raw_current and detect_monthly_payment(raw_current):
            product.is_monthly_payment = True
            product.extraction_warnings.append("price_text_has_monthly_pattern")
        if self._has_monthly_payment_zone(soup):
            product.is_monthly_payment = True
            if "monthly_zone_detected" not in product.extraction_warnings:
                product.extraction_warnings.append("monthly_zone_detected")

        # --- Variant mismatch contra título esperado ---
        if expected_title and product.title:
            if not _titles_match(expected_title, product.title):
                product.not_publishable_reasons.append("variant_mismatch")
                product.extraction_warnings.append("title_mismatch_with_expected")

        # --- Confianza ---
        product.extraction_confidence = self._compute_confidence(product)

        # --- Gates duros ---
        product.not_publishable_reasons.extend(self._not_publishable_reasons(product))
        product.is_publishable = (
            len(product.not_publishable_reasons) == 0
            and product.title is not None
            and product.image_url is not None
            and product.current_price is not None
        )

        return product

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_discount_visible(self, soup: BeautifulSoup) -> Optional[float]:
        # ML típicamente muestra el descuento dentro de andes-money-amount__discount
        for el in soup.find_all(
            class_=lambda c: c and "andes-money-amount__discount" in c
        ):
            text = el.get_text(strip=True)
            pct = extract_discount_percent(text)
            if pct and pct >= 5:
                return float(pct)
        # Fallback: cualquier elemento con texto "% OFF"
        for el in soup.find_all(string=re.compile(r"\d{1,3}\s*%\s*(OFF|de descuento)", re.IGNORECASE)):
            pct = extract_discount_percent(str(el))
            if pct and pct >= 5:
                return float(pct)
        return None

    def _extract_image(self, soup: BeautifulSoup) -> Optional[str]:
        selectors = [
            "figure.ui-pdp-gallery__figure img",
            ".ui-pdp-gallery img",
            "img.ui-pdp-image",
            ".ui-pdp-image",
        ]
        for sel in selectors:
            for img in soup.select(sel):
                for attr in ("data-zoom", "data-src", "src"):
                    value = img.get(attr)
                    if isinstance(value, str) and value.startswith("http"):
                        return value
        return None

    def _extract_availability(self, soup: BeautifulSoup) -> Optional[str]:
        selectors = [
            ".ui-pdp-stock-information__title",
            ".ui-pdp-stock-information",
            ".ui-pdp-buybox__quantity__available",
            ".ui-pdp-color--RED",
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
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
            if "stock disponible" in lower or "disponible" in lower:
                return True

        # Detección de "publicación finalizó" en cualquier parte de la página.
        body_text = soup.get_text(" ", strip=True).lower()
        if any(token in body_text for token in _OUT_OF_STOCK_TOKENS):
            return False

        # Botones de compra → en stock.
        if soup.select_one(".andes-button--loud, button.ui-pdp-action") and not soup.select_one(
            ".ui-pdp-color--RED"
        ):
            return True
        return None

    def _extract_seller(self, soup: BeautifulSoup) -> Optional[str]:
        selectors = [
            ".ui-pdp-seller__link-trigger",
            ".ui-pdp-seller__header__title",
            ".ui-pdp-seller__title",
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(strip=True)
                if text:
                    return text
        return None

    def _extract_brand(self, soup: BeautifulSoup, title: Optional[str]) -> Optional[str]:
        # Ficha técnica: "Marca"
        for row in soup.select(".ui-pdp-specs__table tr, .ui-vpp-highlighted-specs tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) >= 2:
                key = cells[0].get_text(strip=True).lower()
                if "marca" in key:
                    val = cells[1].get_text(strip=True)
                    if val:
                        return val.split()[0].lower()
        if not title:
            return None
        first = title.split()[0]
        return first.lower() if len(first) >= 2 else None

    def _extract_breadcrumb_last(self, soup: BeautifulSoup) -> Optional[str]:
        for sel in (".andes-breadcrumb__link", ".andes-breadcrumb a", "[class*='breadcrumb'] a"):
            crumbs = soup.select(sel)
            if crumbs:
                last = crumbs[-1].get_text(strip=True)
                if last:
                    return last.lower()
        return None

    def _infer_condition(self, soup: BeautifulSoup, title: Optional[str]) -> str:
        # ML expone subtítulo "Nuevo" / "Usado" / "Reacondicionado" en la ficha.
        subtitle = soup.select_one(".ui-pdp-subtitle")
        text = subtitle.get_text(strip=True).lower() if subtitle else ""
        if "reacondic" in text:
            return "refurbished"
        if "usado" in text:
            return "used"
        if "nuevo" in text:
            return "new"

        # Fallback por keyword en título
        title_lower = (title or "").lower()
        if any(token in title_lower for token in _USED_CONDITION_TOKENS):
            if "reacondic" in title_lower or "refurbished" in title_lower:
                return "refurbished"
            return "used"

        # Default
        return "new"

    def _extract_variants(self, soup: BeautifulSoup) -> dict:
        signals: dict[str, str] = {}
        for sel in (
            ".ui-pdp-variations__name",
            ".ui-pdp-variations__picker",
            ".ui-pdp-variations__label",
        ):
            el = soup.select_one(sel)
            if el:
                text = el.get_text(" ", strip=True)
                if text:
                    signals[sel.replace(".", "").replace(" ", "_")] = text
        return signals

    def _has_monthly_payment_zone(self, soup: BeautifulSoup) -> bool:
        for sel in (
            ".ui-pdp-payment-installments",
            ".ui-pdp-installments",
            ".ui-pdp-payment-method-installments",
        ):
            el = soup.select_one(sel)
            if el and detect_monthly_payment(el.get_text(" ", strip=True)):
                return True
        return False

    # ------------------------------------------------------------------
    # JSON-LD / OG fallbacks
    # ------------------------------------------------------------------

    def _is_page_not_found(self, soup: BeautifulSoup, html: str) -> bool:
        """Detecta páginas de 'producto no encontrado' de Mercado Libre.

        Solo marca como not_found cuando la página claramente no tiene producto:
        - Página de error genérica de ML ("parece que esta página no existe")
        - Publicación pausada/finalizada SIN precio (producto dado de baja definitivamente)
        """
        page_text = soup.get_text(separator=" ", strip=True).lower()

        # Error genérico de ML — definitivamente no existe
        if "parece que esta página no existe" in page_text:
            return True

        # Clase CSS de ML para páginas de error 404
        if soup.select_one(".not-found, .ui-search-rescue"):
            return True

        # Publicación pausada/finalizada: solo es not_found si además no hay precio
        # (si hay precio, puede ser out_of_stock temporal)
        paused_texts = [
            "esta publicación fue pausada",
            "el vendedor pausó esta publicación",
        ]
        for text in paused_texts:
            if text in page_text:
                # Verificar si hay precio — si no hay, es not_found
                has_price = bool(soup.select_one(
                    ".andes-money-amount__fraction, "
                    "[class*='price-tag-fraction'], "
                    "[itemprop='price']"
                ))
                if not has_price:
                    return True

        return False

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
                items = offers if isinstance(offers, list) else [offers]
                for offer in items:
                    if not isinstance(offer, dict):
                        continue
                    price = offer.get("price")
                    if price is not None:
                        try:
                            return float(price)
                        except (TypeError, ValueError):
                            continue
        return None

    # ------------------------------------------------------------------
    # Confianza / gates
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
        if product.in_stock is False:
            reasons.append("out_of_stock")
        if product.is_monthly_payment:
            reasons.append("monthly_payment")

        # Usado / reacondicionado: requiere descuento extremo.
        if product.condition in ("used", "refurbished"):
            discount = product.discount_percent or product.calculated_discount_percent or 0
            if discount < self.USED_REQUIRES_DISCOUNT_PERCENT:
                reasons.append("used_requires_extreme_discount")
        return reasons


# ---------------------------------------------------------------------------
# Helpers libres
# ---------------------------------------------------------------------------


def _iter_ld_entries(data):
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
