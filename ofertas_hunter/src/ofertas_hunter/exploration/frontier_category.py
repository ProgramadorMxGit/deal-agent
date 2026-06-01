"""Clasificación de URLs del frontier por categoría + pop category-aware.

Permite a los hunters/discovery priorizar URLs de categorías deficitarias
(según el pool_deficit_plan) sin eliminar el comportamiento legacy.
"""
from __future__ import annotations

import re
from typing import Optional

# Mapea keywords de URL -> categoría canónica (alineado con diversity_metadata).
_URL_CATEGORY_PATTERNS = [
    # belleza ANTES que proteína: "protector-solar" contiene "prote".
    ("belleza", r"protector-solar|bloqueador-solar|filtro-solar|serum|skincare|maquillaje|belleza|cuidado-personal|cosmetic|shampoo|perfume|crema-facial|crema-corporal"),
    ("proteina/suplementos", r"\bprote[ií]na|whey|creatina|suplement|bcaa|gym|fitness-nutricion"),
    ("tecnologia", r"laptop|notebook|ssd|monitor|audifono|celular|smartphone|tablet|computacion|electronica-audio|electronica/|electronica|consola|videojuego|teclado|mouse|gpu|procesador|camara|fuente.de.poder|fuente\+de\+poder|tarjeta.grafica|tarjeta\+grafica|memoria.ram|memoria\+ram|disco.duro|disco.solido|gabinete|enfriamiento|corsair|ryzen|geforce|radeon"),
    ("hogar", r"hogar|cocina|mueble|sarten|licuadora|aspiradora|colchon|electrodomestico|sabana|jardin"),
    ("bebe", r"\bbebe|carriola|andadera|panal|cuna|maternidad|bebes\b"),
    ("herramientas", r"herramient|taladro|construccion|sierra"),
    ("ropa", r"\bropa|playera|pantalon|camisa|moda|vestido|ropa-y-accesorios"),
    ("calzado", r"tenis|zapato|calzado|sneaker"),
    ("despensa", r"supermercado|despensa|alimentos|bebidas|abarrotes"),
    ("juguetes", r"juguete|juegos-y-juguetes|lego"),
    ("mascotas", r"mascota|perro|gato|animales-y-mascotas"),
    ("deportes", r"deporte|fitness|bicicleta"),
    ("auto", r"\bauto|refaccion|moto|llanta|vehiculo"),
]


def category_of_url(url: str) -> str:
    u = (url or "").lower()
    for name, rx in _URL_CATEGORY_PATTERNS:
        if re.search(rx, u):
            return name
    return "otro"


def adjusted_score(
    base_score: float,
    category: str,
    deficit_categories: Optional[list],
    saturated_categories: Optional[list],
    *,
    deficit_boost: float = 2.0,
    saturated_penalty: float = 0.5,
) -> float:
    """Ajusta el score de una URL según déficit/saturación de su categoría."""
    score = base_score
    if deficit_categories and category in deficit_categories:
        score *= deficit_boost
    if saturated_categories and category in saturated_categories:
        score *= saturated_penalty
    return score


__all__ = ["category_of_url", "adjusted_score"]
