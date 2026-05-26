"""Utilidades genéricas de parsing de precios.

Reutilizadas por Amazon y Mercado Libre. **Puras**: input string → output
float/int. Sin red, sin DOM.

Soporta:
- formato MX: `$1,299.00` → 1299.0
- formato europeo: `1.299,00` → 1299.0
- decimales / sin decimales
- detección de mensualidades (`/mes`, `mensuales`, `por mes`, `meses`, `desde`)
"""

from __future__ import annotations

import re
from typing import Optional


_MONTHLY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bpor\s+mes\b",
        r"\bal\s+mes\b",
        r"/\s*mes\b",
        r"\bmensual(es|idad|idades)?\b",
        r"\bmsi\b",
        r"\b\d+\s*meses\s+sin\s+intereses\b",
        r"\b\d+\s*pagos\s+de\b",
        r"\bdesde\s+\$\s*\d",
        r"\b\d+\s*x\s+\$\s*\d",
        r"\b\$\s*\d+\s*/\s*mes\b",
    )
)


def parse_price_text(text: Optional[str]) -> Optional[float]:
    """Convierte texto de precio a float.

    Devuelve `None` si no logra interpretar.
    """
    if not text:
        return None
    cleaned = re.sub(r"[^\d,.]", "", text.strip())
    if not cleaned:
        return None
    has_comma = "," in cleaned
    has_dot = "." in cleaned

    if has_comma and has_dot:
        # Formato europeo si la coma viene después del punto.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif has_comma and not has_dot:
        parts = cleaned.split(",")
        if len(parts[-1]) == 2:
            cleaned = cleaned.replace(",", ".")  # decimal europeo
        else:
            cleaned = cleaned.replace(",", "")  # miles
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_discount_percent(text: Optional[str]) -> Optional[int]:
    """Extrae porcentaje (1-99) de un texto. `'-65%'` → 65."""
    if not text:
        return None
    match = re.search(r"(\d{1,3})\s*%", text)
    if match:
        value = int(match.group(1))
        if 1 <= value <= 99:
            return value
    return None


def calculate_discount(
    current: Optional[float], previous: Optional[float]
) -> Optional[float]:
    if current is None or previous is None or previous <= 0:
        return None
    if current >= previous:
        return 0.0
    return round((1 - current / previous) * 100, 2)


def detect_monthly_payment(text: Optional[str]) -> bool:
    """True si el texto sugiere mensualidad / MSI / pago mensual."""
    if not text:
        return False
    for pattern in _MONTHLY_PATTERNS:
        if pattern.search(text):
            return True
    return False
