"""Utilidades de URLs para marketplaces.

Funciones puras (sin red) usadas por hunters/revalidators.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse, urlunparse


_AMAZON_HOSTS: tuple[str, ...] = (
    "amazon.com.mx",
    "www.amazon.com.mx",
    "amazon.com",
    "www.amazon.com",
    "amzn.to",
    "amzn.mx",
)


_ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE)


def is_amazon_url(url: Optional[str]) -> bool:
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in _AMAZON_HOSTS)


def extract_asin(url: Optional[str]) -> Optional[str]:
    """Extrae ASIN de URLs típicas de Amazon."""
    if not url:
        return None
    match = _ASIN_RE.search(url)
    if match:
        return match.group(1).upper()
    return None


def canonicalize_amazon_url(url: str) -> str:
    """Devuelve la URL canónica `https://www.amazon.com.mx/dp/<ASIN>`.

    Si no se puede extraer ASIN, devuelve la URL original sin tracking
    params conocidos.
    """
    asin = extract_asin(url)
    if asin:
        return f"https://www.amazon.com.mx/dp/{asin}"
    parsed = urlparse(url)
    return urlunparse(
        (
            parsed.scheme or "https",
            parsed.netloc or "www.amazon.com.mx",
            parsed.path.rstrip("/"),
            "",
            "",
            "",
        )
    )
