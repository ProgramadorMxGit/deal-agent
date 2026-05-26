"""Extractores de URLs desde páginas de listing / categoría / deals.

Reciben HTML como string y devuelven listas de `ClassifiedUrl` ya tipadas.
**Puros**: sin red, sin Playwright. Esto los hace testables con fixtures.

Soporta:
- Amazon: `/dp/ASIN`, paginación `/s?...&page=N`.
- Mercado Libre: `/p/MLM...`, `/MLM...`, navegación de categorías.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .url_classifier import ClassifiedUrl, classify


_DEFAULT_AMAZON_BASE = "https://www.amazon.com.mx"
_DEFAULT_ML_BASE = "https://www.mercadolibre.com.mx"


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

    for a in soup.select("a[href*='/dp/']"):
        href = a.get("href")
        if not href:
            continue
        url = _normalize(href, base)
        info = classify(url)
        if info.kind == "product":
            # Productos en /deals tienen prioridad mayor
            discovered.append(
                ClassifiedUrl(
                    url=info.url, marketplace=info.marketplace, kind=info.kind, score=12.0
                )
            )
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
