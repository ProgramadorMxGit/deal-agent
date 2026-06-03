"""Mapeo de categorías (del category_deficit_planner) a URLs de OFERTAS.

El planner recomienda categorías deficitarias para mantener diversidad. En vez
de sembrar listados de categoría GENÉRICA (que traen productos a precio normal),
traducimos cada categoría a su página de OFERTAS de Mercado Libre
(`/ofertas/<slug>`), donde las tarjetas sí vienen con descuento.

Así se mantiene la diversidad de categorías PERO sobre ofertas reales.
"""

from __future__ import annotations

from typing import Iterable

_ML_OFERTAS_BASE = "https://www.mercadolibre.com.mx/ofertas"

# Categoría canónica (diversity_metadata / planner) -> slug de la sección de
# ofertas de ML. Si una categoría no tiene slug propio en /ofertas, se cae al
# hub general de ofertas (que igual filtra por descuento).
_CATEGORY_TO_ML_OFFERS_SLUG: dict[str, str] = {
    "tecnologia": "tecnologia",
    "hogar": "hogar",
    "bebe": "bebes",
    "herramientas": "herramientas-construccion",
    "ropa": "moda",
    "calzado": "moda",
    "despensa": "supermercado",
    "juguetes": "juguetes",
    "mascotas": "mascotas",
    "belleza": "belleza-cuidado-personal",
    "proteina/suplementos": "salud",
    "deportes": "deportes-fitness",
    "auto": "accesorios-para-vehiculos",
}


def ml_offers_url_for_category(category: str) -> str:
    """Devuelve la URL de ofertas ML para una categoría. Hub general si no mapea."""
    slug = _CATEGORY_TO_ML_OFFERS_SLUG.get((category or "").strip().lower())
    if not slug:
        return _ML_OFERTAS_BASE
    return f"{_ML_OFERTAS_BASE}/{slug}"


def ml_offers_urls_for_categories(categories: Iterable[str]) -> list[str]:
    """Lista (sin duplicados, en orden) de URLs de ofertas para las categorías."""
    seen: set[str] = set()
    out: list[str] = []
    for cat in categories or []:
        url = ml_offers_url_for_category(cat)
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


__all__ = [
    "ml_offers_url_for_category",
    "ml_offers_urls_for_categories",
    "_CATEGORY_TO_ML_OFFERS_SLUG",
]
