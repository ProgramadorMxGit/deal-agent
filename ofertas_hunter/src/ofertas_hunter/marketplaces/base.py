"""Tipos comunes para los marketplaces (Amazon, Mercado Libre, ...)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExtractedProduct:
    """Resultado de extraer datos de la página de un producto.

    Campos opcionales si el extractor no logró encontrarlos. `extraction_warnings`
    lista los puntos donde se cayó a fallback (og:image, json-ld, regex, etc.).
    """

    url: str
    canonical_url: str
    marketplace: str

    title: Optional[str] = None
    current_price: Optional[float] = None
    previous_price: Optional[float] = None
    discount_percent: Optional[float] = None
    calculated_discount_percent: Optional[float] = None
    image_url: Optional[str] = None
    availability: Optional[str] = None
    in_stock: Optional[bool] = None
    seller: Optional[str] = None
    condition: str = "new"
    brand_guess: Optional[str] = None
    category_guess: Optional[str] = None
    asin: Optional[str] = None
    selected_variant_signals: dict = field(default_factory=dict)
    raw_price_text: Optional[str] = None
    raw_previous_price_text: Optional[str] = None

    extraction_confidence: str = "low"  # low | medium | high
    extraction_warnings: list[str] = field(default_factory=list)
    is_monthly_payment: bool = False
    is_publishable: bool = False
    not_publishable_reasons: list[str] = field(default_factory=list)
