"""Extractores de URLs desde páginas de listing / categoría / deals.

EXTRACTOR_V2: soporta deal grids Amazon (data-asin), poly-card ML y promotion-item.

Reciben HTML como string y devuelven listas de `ClassifiedUrl` ya tipadas.
**Puros**: sin red, sin Playwright. Esto los hace testables con fixtures.

Soporta:
- Amazon: `/dp/ASIN`, paginación `/s?...&page=N`.
- Mercado Libre: `/p/MLM...`, `/MLM...`, navegación de categorías.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..extraction.price_parser import calculate_discount, extract_discount_percent
from .url_classifier import ClassifiedUrl, classify


_DEFAULT_AMAZON_BASE = "https://www.amazon.com.mx"
_DEFAULT_ML_BASE = "https://www.mercadolibre.com.mx"


@dataclass
class MercadoLibreListingItem:
    """Item extraído a nivel de tarjeta (card) de un listing ML.

    Contiene metadata mínima de precio/descuento cuando está visible en la
    tarjeta, para permitir un prefiltro temprano antes de pagar el costo de
    abrir la página del producto con Playwright. Todos los campos de precio
    son opcionales: el listing no siempre los expone.
    """

    url: str
    kind: str
    title: Optional[str] = None
    current_price: Optional[float] = None
    original_price: Optional[float] = None
    discount_percent: Optional[float] = None
    raw_discount_text: Optional[str] = None


def _normalize(url: str, base: str) -> str:
    """Convierte hrefs relativos en absolutos. Strip de fragments."""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http"):
        url = urljoin(base, url)
    # quitar fragment
    if "#" in url:
        url = url.split("#", 1)[0]
    return url


def _dedup_classified(items: Iterable[ClassifiedUrl]) -> list[ClassifiedUrl]:
    seen: set[str] = set()
    out: list[ClassifiedUrl] = []
    for it in items:
        if not it.url or it.url in seen:
            continue
        seen.add(it.url)
        out.append(it)
    return out


# ---------------------------------------------------------------------------
# Amazon
# ---------------------------------------------------------------------------


def extract_amazon_listing(html: str, base_url: Optional[str] = None) -> list[ClassifiedUrl]:
    """Extrae URLs de productos y paginación desde una página `/s?k=...`.

    Devuelve una lista de `ClassifiedUrl` con kind in {product, listing}.
    """
    if not html:
        return []
    base = base_url or _DEFAULT_AMAZON_BASE
    soup = BeautifulSoup(html, "html.parser")
    discovered: list[ClassifiedUrl] = []

    # Productos: data-component-type='s-search-result' tienen un h2 > a hacia /dp/
    for container in soup.select("[data-component-type='s-search-result']"):
        # Algunos resultados tienen data-asin directo
        asin = container.get("data-asin")
        if asin and len(asin) == 10:
            discovered.append(
                ClassifiedUrl(
                    url=f"{_DEFAULT_AMAZON_BASE}/dp/{asin}",
                    marketplace="amazon",
                    kind="product",
                    score=10.0,
                )
            )
            continue
        # Fallback: cualquier <a> con /dp/
        a = container.select_one("h2 a, a.a-link-normal[href*='/dp/']")
        if a and a.get("href"):
            url = _normalize(a["href"], base)
            info = classify(url)
            if info.kind == "product":
                discovered.append(info)

    # Paginación: <a class="s-pagination-next" href="...">
    for a in soup.select("a.s-pagination-next, a.s-pagination-item"):
        href = a.get("href")
        if not href or "disabled" in (a.get("aria-disabled") or ""):
            continue
        url = _normalize(href, base)
        info = classify(url)
        if info.kind == "listing":
            discovered.append(info)

    return _dedup_classified(discovered)


def extract_amazon_deals(html: str, base_url: Optional[str] = None) -> list[ClassifiedUrl]:
    """Extrae URLs de productos desde `/deals` y `/gp/goldbox`."""
    if not html:
        return []
    base = base_url or _DEFAULT_AMAZON_BASE
    soup = BeautifulSoup(html, "html.parser")
    discovered: list[ClassifiedUrl] = []

    # EXTRACTOR_V2: deal grids + search cards modernas.
    # (a) data-asin en cualquier contenedor (deals grid / search result).
    for container in soup.select("[data-asin]"):
        asin = (container.get("data-asin") or "").strip()
        if asin and len(asin) == 10 and asin.isalnum():
            discovered.append(
                ClassifiedUrl(
                    url=f"{_DEFAULT_AMAZON_BASE}/dp/{asin}",
                    marketplace="amazon", kind="product", score=12.0,
                )
            )
    # (b) cualquier <a> con /dp/ASIN (carruseles, grids, links directos).
    for a in soup.select("a[href*='/dp/']"):
        href = a.get("href")
        if not href:
            continue
        url = _normalize(href, base)
        info = classify(url)
        if info.kind == "product":
            discovered.append(
                ClassifiedUrl(
                    url=info.url, marketplace=info.marketplace, kind=info.kind, score=12.0
                )
            )
    # (c) paginación de búsquedas filtradas (mantener recurrencia de deal search).
    for a in soup.select("a.s-pagination-next, a.s-pagination-item"):
        href = a.get("href")
        if not href or "disabled" in (a.get("aria-disabled") or ""):
            continue
        info = classify(_normalize(href, base))
        if info.kind == "listing":
            discovered.append(info)
    return _dedup_classified(discovered)


# ---------------------------------------------------------------------------
# Mercado Libre
# ---------------------------------------------------------------------------


def extract_mercadolibre_listing(
    html: str, base_url: Optional[str] = None
) -> list[ClassifiedUrl]:
    """Extrae URLs de productos y categorías desde un listing/categoría ML."""
    if not html:
        return []
    base = base_url or _DEFAULT_ML_BASE
    soup = BeautifulSoup(html, "html.parser")
    discovered: list[ClassifiedUrl] = []

    # Productos en cards
    for a in soup.select(
        "a.ui-search-item__group__element, a.ui-search-link, "
        "a.poly-component__title, a.promotion-item__link-container, "
        "a[href*='/p/MLM'], a[href*='/MLM']"
    ):
        href = a.get("href")
        if not href:
            continue
        url = _normalize(href, base)
        info = classify(url)
        if info.kind == "product":
            discovered.append(info)
        elif info.kind in ("listing", "category"):
            # links a sub-categorías o filtros
            discovered.append(info)

    # Paginación
    for a in soup.select("a.andes-pagination__link, li.andes-pagination__button a"):
        href = a.get("href")
        if not href:
            continue
        url = _normalize(href, base)
        info = classify(url)
        if info.kind in ("listing", "category"):
            discovered.append(info)

    return _dedup_classified(discovered)


# ---------------------------------------------------------------------------
# Mercado Libre — extracción a nivel de tarjeta (card) con metadata de precio
# ---------------------------------------------------------------------------


_ML_CARD_SELECTORS = (
    "li.ui-search-layout__item",
    "div.ui-search-result__wrapper",
    "div.poly-card",
    "div.andes-card",
    "li.promotion-item",
    "div.promotion-item",
    "ol.items_container > li",
    "div.ui-recommendations-card",
)

_ML_PRODUCT_LINK_SELECTORS = (
    "a.poly-component__title",
    "a.ui-search-link",
    "a.ui-search-item__group__element",
    "a.promotion-item__link-container",
    "a.ui-recommendations-card__link",
    "a[href*='/p/MLM']",
    "a[href*='/MLM']",
)

_OFF_TEXT_RE = re.compile(r"\d{1,3}\s*%\s*(?:OFF|de descuento)", re.IGNORECASE)


def _amount_from_card_element(el) -> Optional[float]:
    """Combina `andes-money-amount__fraction` + `__cents` dentro de un nodo."""
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


def _extract_card_discount_text(card) -> Optional[str]:
    """Texto de descuento visible en la card (`50% OFF`), si existe."""
    discount_el = card.find(
        class_=lambda c: c and "andes-money-amount__discount" in c
    )
    if discount_el:
        text = discount_el.get_text(strip=True)
        if text:
            return text
    # Fallback: cualquier texto "% OFF" / "% de descuento" dentro de la card.
    match = card.find(string=_OFF_TEXT_RE)
    if match:
        return str(match).strip()
    return None


def _extract_card_prices(card) -> tuple[Optional[float], Optional[float]]:
    """Devuelve (current_price, original_price) leídos de la card.

    El precio anterior suele venir tachado (`<s>` o `--previous`); el actual
    es el primer `andes-money-amount` que no sea ni previous ni discount.

    Usamos selectores CSS de clase (token exacto) para no confundir los
    contenedores `andes-money-amount` con los spans internos
    `andes-money-amount__fraction` / `__cents`.
    """
    original = None
    previous_el = card.select_one(".andes-money-amount--previous")
    if previous_el is None:
        previous_el = card.find("s")
    if previous_el is not None:
        original = _amount_from_card_element(previous_el)

    current = None
    for el in card.select(".andes-money-amount"):
        classes = el.get("class") or []
        if "andes-money-amount--previous" in classes:
            continue
        if any("__discount" in c for c in classes):
            continue
        val = _amount_from_card_element(el)
        if val is not None:
            current = val
            break

    return current, original


def extract_mercadolibre_listing_items(
    html: str, base_url: Optional[str] = None
) -> list[MercadoLibreListingItem]:
    """Extrae items (con metadata de precio/descuento) desde un listing ML.

    A diferencia de `extract_mercadolibre_listing` (que devuelve sólo URLs
    clasificadas), esta función trabaja a nivel de **tarjeta**: por cada card
    intenta leer el link del producto y, si están visibles, el descuento, el
    precio actual y el precio anterior. Esto habilita un prefiltro temprano.

    Mantiene el flujo existente intacto: es una función nueva y aditiva.
    """
    if not html:
        return []
    base = base_url or _DEFAULT_ML_BASE
    soup = BeautifulSoup(html, "html.parser")

    cards = []
    for sel in _ML_CARD_SELECTORS:
        found = soup.select(sel)
        if found:
            cards = found
            break

    items: list[MercadoLibreListingItem] = []
    seen: set[str] = set()

    for card in cards:
        link = None
        for sel in _ML_PRODUCT_LINK_SELECTORS:
            link = card.select_one(sel)
            if link and link.get("href"):
                break
        if not link or not link.get("href"):
            continue

        url = _normalize(link["href"], base)
        info = classify(url)
        if info.kind != "product":
            continue
        if info.url in seen:
            continue
        seen.add(info.url)

        title = link.get("title") or link.get_text(strip=True) or None

        current, original = _extract_card_prices(card)

        raw_discount_text = _extract_card_discount_text(card)
        discount_percent: Optional[float] = None
        pct = extract_discount_percent(raw_discount_text) if raw_discount_text else None
        if pct is not None:
            discount_percent = float(pct)
        else:
            calc = calculate_discount(current, original)
            if calc is not None and calc > 0:
                discount_percent = float(calc)

        items.append(
            MercadoLibreListingItem(
                url=info.url,
                kind="product",
                title=title,
                current_price=current,
                original_price=original,
                discount_percent=discount_percent,
                raw_discount_text=raw_discount_text,
            )
        )

    return items
