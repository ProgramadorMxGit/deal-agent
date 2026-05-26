"""Calcula descuentos reales a partir de precios y referencias."""

from __future__ import annotations

from typing import Optional


def calculate_discount(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    """Porcentaje de descuento entre `previous` y `current`.

    Devuelve `None` si falta uno de los dos o si `previous <= 0`.
    Devuelve `0.0` si `current >= previous`.
    """
    if current is None or previous is None or previous <= 0:
        return None
    if current >= previous:
        return 0.0
    return round((1 - current / previous) * 100, 2)


def has_minimum_discount(
    current: Optional[float], previous: Optional[float], minimum: float
) -> bool:
    """True si el descuento calculado es >= `minimum` (en porcentaje 0..100)."""
    pct = calculate_discount(current, previous)
    if pct is None:
        return False
    return pct >= minimum
