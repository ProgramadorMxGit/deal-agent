"""Rangos heurísticos por categoría — primera aproximación.

Estos valores son la base inicial. El bot debe aprender mejores rangos a
partir de su histórico (`category_price_ranges` en SQLite).

Ver `docs/PRICE_ERROR_DETECTION.md §5`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CategoryRange:
    """Umbrales de "muy sospechoso" por categoría.

    Si el `current_price` del producto está por debajo de
    `very_suspicious_below`, eso aporta una fuerte señal de error de precio.
    `extreme_below` indica precios casi imposibles que disparan
    confianza muy alta.
    """

    category: str
    suspicious_below: Optional[float]
    very_suspicious_below: Optional[float]
    extreme_below: Optional[float]


PREMIUM_BRANDS: frozenset[str] = frozenset(
    {
        "apple",
        "samsung",
        "sony",
        "dell",
        "hp",
        "lenovo",
        "asus",
        "msi",
        "microsoft",
        "bose",
        "huawei",
        "lg",
        "gigabyte",
    }
)

HIGH_VALUE_CATEGORIES: frozenset[str] = frozenset(
    {
        "laptop",
        "smartphone",
        "tablet",
        "audio_premium",
        "console",
        "pc_components",
        "display",
    }
)


# Tabla base de rangos. Las columnas se interpretan como límites de "precio
# anormalmente bajo" según la spec.
RANGES: dict[str, CategoryRange] = {
    # Laptops
    "laptop": CategoryRange(
        category="laptop",
        suspicious_below=4000.0,
        very_suspicious_below=6000.0,
        extreme_below=2000.0,
    ),
    "laptop_gaming": CategoryRange(
        category="laptop_gaming",
        suspicious_below=6000.0,
        very_suspicious_below=8000.0,
        extreme_below=3500.0,
    ),
    "laptop_business": CategoryRange(
        category="laptop_business",
        suspicious_below=4500.0,
        very_suspicious_below=5000.0,
        extreme_below=2500.0,
    ),
    # Smartphones
    "smartphone": CategoryRange(
        category="smartphone",
        suspicious_below=5000.0,
        very_suspicious_below=7000.0,
        extreme_below=500.0,
    ),
    "smartphone_flagship": CategoryRange(
        category="smartphone_flagship",
        suspicious_below=7000.0,
        very_suspicious_below=8000.0,
        extreme_below=4000.0,
    ),
    # Tablets
    "tablet": CategoryRange(
        category="tablet",
        suspicious_below=5000.0,
        very_suspicious_below=6000.0,
        extreme_below=3000.0,
    ),
    # Audio premium
    "audio_premium": CategoryRange(
        category="audio_premium",
        suspicious_below=1500.0,
        very_suspicious_below=1500.0,
        extreme_below=800.0,
    ),
    # Componentes
    "pc_components": CategoryRange(
        category="pc_components",
        suspicious_below=None,
        very_suspicious_below=None,
        extreme_below=None,
    ),
    # Display
    "display": CategoryRange(
        category="display",
        suspicious_below=None,
        very_suspicious_below=None,
        extreme_below=None,
    ),
    # Console
    "console": CategoryRange(
        category="console",
        suspicious_below=None,
        very_suspicious_below=None,
        extreme_below=None,
    ),
    # Apparel: usado como categoría neutra, sólo gana puntos por descuento explícito.
    "apparel": CategoryRange(
        category="apparel",
        suspicious_below=None,
        very_suspicious_below=None,
        extreme_below=None,
    ),
    "uncategorized": CategoryRange(
        category="uncategorized",
        suspicious_below=None,
        very_suspicious_below=None,
        extreme_below=None,
    ),
}


def get_range(category: Optional[str]) -> CategoryRange:
    """Devuelve los umbrales por categoría, con fallback a 'uncategorized'."""
    if not category:
        return RANGES["uncategorized"]
    return RANGES.get(category, RANGES["uncategorized"])
