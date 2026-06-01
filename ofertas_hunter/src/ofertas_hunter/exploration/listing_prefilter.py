"""Prefiltro temprano de descuento para items de listing.

Decide si un `MercadoLibreListingItem` debe entrar al frontier **antes** de
pagar el costo de abrir la página del producto con Playwright.

Reglas (en orden):
1. Si el item NO es `product` (listing/category/deals) → se acepta como
   navegación (no se filtra por precio).
2. Si `discount_percent >= min_discount` → aceptar (`accepted_discount`).
3. Si `discount_percent` existe y es menor al umbral → rechazar
   (`discarded_low_discount`).
4. Si `discount_percent is None`:
   - `strict=True` → rechazar (`discarded_unknown_strict`).
   - `strict=False` → aceptar con score bajo (`accepted_unknown`).

Este prefiltro es una **optimización**, no la fuente de verdad: el filtro
real sigue en `hunt_one()` sobre la página del producto.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .listing_extractor import MercadoLibreListingItem


# Score por defecto para productos que SÍ pasan el prefiltro por descuento.
DEFAULT_PRODUCT_SCORE = 10.0


@dataclass(frozen=True)
class PrefilterDecision:
    accept: bool
    bucket: str
    score: float = 0.0


def decide_listing_item(
    item: MercadoLibreListingItem,
    *,
    min_discount: float,
    strict: bool = False,
    unknown_score: float = 1.0,
    product_score: float = DEFAULT_PRODUCT_SCORE,
) -> PrefilterDecision:
    """Decide si `item` entra al frontier. Ver reglas en el docstring del módulo."""
    if item.kind != "product":
        return PrefilterDecision(accept=True, bucket="accepted_navigational", score=0.0)

    discount = item.discount_percent

    if discount is None:
        if strict:
            return PrefilterDecision(
                accept=False, bucket="discarded_unknown_strict", score=0.0
            )
        return PrefilterDecision(
            accept=True, bucket="accepted_unknown", score=unknown_score
        )

    if discount >= min_discount:
        return PrefilterDecision(
            accept=True, bucket="accepted_discount", score=product_score
        )

    return PrefilterDecision(
        accept=False, bucket="discarded_low_discount", score=0.0
    )
