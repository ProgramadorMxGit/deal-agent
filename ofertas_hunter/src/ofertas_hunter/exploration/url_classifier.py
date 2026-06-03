"""Clasificador de URLs por marketplace.

Tipos:
- `product`: página de producto individual (ej: `/dp/ASIN`, `/MLM12345678`).
- `listing`: resultados de búsqueda o categoría con productos
  (ej: `/s?k=...`, `listado.mercadolibre.com.mx/...`).
- `category`: hub de categoría (sin filtro de búsqueda).
- `deals`: páginas de ofertas (`/deals`, `/ofertas`).
- `unknown`: cualquier otra cosa (login, ayuda, blog, etc.).

Cada hunter usa este clasificador para decidir qué hacer con una URL nueva
descubierta.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


_AMAZON_PRODUCT_RE = re.compile(
    r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE
)
_AMAZON_SEARCH_RE = re.compile(r"/s\?", re.IGNORECASE)
_AMAZON_DEALS_RE = re.compile(r"/(?:deals|gp/goldbox)", re.IGNORECASE)
_AMAZON_BLOCKED_RE = re.compile(
    r"/(?:gp/help|gp/cart|gp/wishlist|gp/css|ap/signin|ap/register|errors/)",
    re.IGNORECASE,
)


_ML_PRODUCT_PATTERNS = (
    re.compile(r"/p/MLM\d+"),
    re.compile(r"/MLM\d{8,}"),
)
_ML_LISTING_PATTERNS = (
    re.compile(r"listado\.mercadolibre\.com\.mx"),
    re.compile(r"mercadolibre\.com\.mx/[a-z][a-z0-9-]+\?"),
)
_ML_CATEGORY_PATTERNS = (
    re.compile(r"mercadolibre\.com\.mx/c/[a-z0-9][a-z0-9-]*"),
    re.compile(r"mercadolibre\.com\.mx/[a-z][a-z0-9-]+/?$"),
    re.compile(r"mercadolibre\.com\.mx/[a-z][a-z0-9-]+/[a-z][a-z0-9-]+/?$"),
)
_ML_DEALS_PATTERNS = (
    re.compile(r"mercadolibre\.com\.mx/ofertas"),
    re.compile(r"mercadolibre\.com\.mx/c/.+ofertas"),
    re.compile(r"mercadolibre\.com\.mx/mas-vendidos"),
)

# Señales de que un LISTADO (listado.mercadolibre.com.mx/...) es de ofertas:
# trae filtro de descuento o está en una sección de promociones. Estos listados
# sí valen score alto porque sus tarjetas vienen con descuento.
_ML_LISTING_DEAL_SIGNALS = re.compile(
    r"_Descuento_|_Deal|tier=deal|promociones|/ofertas",
    re.IGNORECASE,
)


_ML_BLOCKED_PREFIXES = frozenset({
    "mis-alertas", "preferencias-de-venta", "my-reviews", "mis-compras",
    "mis-ventas", "perfil", "suscripciones", "notificaciones", "mensajes",
    "preguntas", "facturacion", "creditos", "mis-favoritos", "historial",
    "configuracion", "seguridad", "mi-cuenta", "account",
    "login", "logout", "registro", "signup", "registration", "jms", "gz",
    "ayuda", "help", "terminos", "privacidad", "cookies", "sitemap",
    "noticias", "prensa", "institucional", "legal", "blog",
    "vendedor", "seller", "publicar", "vender", "publicaciones", "ventas",
    "compras", "afiliados", "hub", "accesibilidad", "shorts", "clips",
})


@dataclass(frozen=True)
class ClassifiedUrl:
    url: str
    marketplace: str   # amazon | mercadolibre | other
    kind: str          # product | listing | category | deals | unknown
    score: float = 0.0  # prioridad (más alto = más útil)


def classify(url: str) -> ClassifiedUrl:
    """Clasifica una URL determinísticamente."""
    if not url or not isinstance(url, str):
        return ClassifiedUrl(url=url or "", marketplace="other", kind="unknown")

    try:
        parsed = urlparse(url)
    except Exception:
        return ClassifiedUrl(url=url, marketplace="other", kind="unknown")

    host = (parsed.hostname or "").lower()
    if "amazon.com.mx" in host or "amazon.com" in host or host in ("amzn.to", "amzn.mx"):
        return _classify_amazon(url, parsed)
    if "mercadolibre.com.mx" in host or host == "meli.la" or host.endswith(".meli.la"):
        return _classify_mercadolibre(url, parsed)
    return ClassifiedUrl(url=url, marketplace="other", kind="unknown")


def _classify_amazon(url: str, parsed) -> ClassifiedUrl:
    if _AMAZON_BLOCKED_RE.search(url):
        return ClassifiedUrl(url=url, marketplace="amazon", kind="unknown")
    if _AMAZON_PRODUCT_RE.search(url):
        return ClassifiedUrl(url=url, marketplace="amazon", kind="product", score=10.0)
    if _AMAZON_DEALS_RE.search(url):
        return ClassifiedUrl(url=url, marketplace="amazon", kind="deals", score=5.0)
    if _AMAZON_SEARCH_RE.search(url):
        return ClassifiedUrl(url=url, marketplace="amazon", kind="listing", score=3.0)
    return ClassifiedUrl(url=url, marketplace="amazon", kind="unknown")


def _classify_mercadolibre(url: str, parsed) -> ClassifiedUrl:
    path = parsed.path.lstrip("/")
    first_segment = path.split("/", 1)[0].lower()
    if first_segment in _ML_BLOCKED_PREFIXES:
        return ClassifiedUrl(url=url, marketplace="mercadolibre", kind="unknown")

    for p in _ML_PRODUCT_PATTERNS:
        if p.search(url):
            return ClassifiedUrl(url=url, marketplace="mercadolibre", kind="product", score=10.0)
    for p in _ML_DEALS_PATTERNS:
        if p.search(url):
            return ClassifiedUrl(url=url, marketplace="mercadolibre", kind="deals", score=8.0)
    for p in _ML_LISTING_PATTERNS:
        if p.search(url):
            # Distinguir listados de OFERTA (con filtro de descuento/promoción)
            # de listados de categoría genérica. Los genéricos traen productos
            # a precio normal y NO deben desplazar a las ofertas en el frontier.
            if _ML_LISTING_DEAL_SIGNALS.search(url):
                return ClassifiedUrl(
                    url=url, marketplace="mercadolibre", kind="deals", score=7.0
                )
            return ClassifiedUrl(
                url=url, marketplace="mercadolibre", kind="listing", score=1.0
            )
    for p in _ML_CATEGORY_PATTERNS:
        if p.search(url):
            return ClassifiedUrl(url=url, marketplace="mercadolibre", kind="category", score=2.0)
    return ClassifiedUrl(url=url, marketplace="mercadolibre", kind="unknown")
