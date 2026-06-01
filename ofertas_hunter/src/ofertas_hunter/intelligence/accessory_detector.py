"""Detector heurístico de accesorios genéricos.

Tres falsos positivos críticos (cargadores 20W "compatible con iPhone")
fueron publicados como ERROR DE PRECIO con confianza `medium`. La causa
raíz: el `_fingerprint_smartphone` aceptaba cualquier título que contuviera
"iphone" y combinaba con el bonus `smartphone_below_500_extreme`, llevando
el score por encima de 60 (`possible_price_error`) en accesorios cuyo
precio normal ronda los $76 - $300 MXN.

Este módulo implementa los gates duros que la spec solicita:

* `is_generic_accessory(title, category, brand)` — True si el título es de
  un accesorio (cargador / cable / funda / protector / mica / case /
  carcasa / soporte / adaptador) o si la categoría declarada es de
  accesorio. No depende de la marca, sólo del título.
* `mentions_compatible_with_premium(title)` — True si el título usa
  fórmulas tipo "compatible con iPhone", "para Samsung", "compatible con
  iPad". Indica que la marca premium está sólo como referencia de
  compatibilidad, no como fabricante real.
* `is_real_premium_product(title, brand)` — True si el título describe un
  producto real de marca premium reconocida (iPhone, iPad, Galaxy S/S
  Ultra/Z, AirPods, Apple Watch, laptop real Dell/HP/Lenovo/ASUS/MSI,
  audífonos flagship Sony WF/WH).

Reglas:

* Si `is_generic_accessory` y al mismo tiempo `mentions_compatible_with_premium`
  → la marca premium NO cuenta para los bonus de "premium brand".
* `is_real_premium_product` debe excluir cualquier título que sea generic
  accessory.

Las listas de keywords están en español (MX) porque ML / Telegram / Amazon MX
publican en español.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Tokens de accesorio genérico (mata indicador)
# ---------------------------------------------------------------------------


_ACCESSORY_TOKENS: tuple[str, ...] = (
    "cargador",
    "cargadores",
    "cable",
    "cables",
    "adaptador",
    "adaptadores",
    "protector",
    "protectores",
    "mica",
    "micas",
    "funda",
    "fundas",
    "case",
    "carcasa",
    "carcasas",
    "soporte",
    "soportes",
    "tripode",
    "trípode",
    "vidrio templado",
    "cristal templado",
    "limpiador",
    "kit limpieza",
    "manos libres",
    "stylus",
    "pluma touch",
    "lapiz touch",
    "lápiz touch",
    "hub usb",
    "splitter",
    "cabeza única",
    "cabeza unica",
    # Audio accesorios (NO son smartphones aunque mencionen marcas como
    # "Redmi Buds", "Galaxy Buds", "JBL Flip", etc.). Esto evita que el
    # scorer los promueva con `smartphone_below_500_extreme`.
    "audífonos",
    "audifonos",
    "auriculares",
    "earbuds",
    "earphones",
    "headphones",
    "headset",
    "audífono",
    "audifono",
    "bocina",
    "bocinas",
    "altavoz",
    "altavoces",
    "speaker",
    "buds",  # Redmi Buds, Galaxy Buds, Pixel Buds, AirPods Buds
    "smartwatch",
    "smart watch",
    "smartband",
    "smart band",
    "pulsera inteligente",
    "reloj inteligente",
    "fitness band",
)


# Compilamos las expresiones con word-boundaries para evitar falsos positivos
# tipo "carcasa" matcheando dentro de palabras más largas. Usamos \b cuando
# es seguro (mayoría de tokens) y `(?<!\w)/(?!\w)` para multi-palabra.
def _compile_token(token: str) -> re.Pattern[str]:
    if " " in token:
        return re.compile(rf"(?<![A-Za-zÁÉÍÓÚáéíóúÑñ]){re.escape(token)}(?![A-Za-zÁÉÍÓÚáéíóúÑñ])", re.IGNORECASE)
    return re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE)


_ACCESSORY_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    _compile_token(t) for t in _ACCESSORY_TOKENS
)

# Indicadores adicionales de cargador/cable (potencia / longitud) que sumados
# a uno de los tokens anteriores refuerzan la clasificación.
_ACCESSORY_SECONDARY_TOKENS: tuple[str, ...] = (
    "20w",
    "30w",
    "65w",
    "100w",
    "120w",
    "1 metro",
    "2 metros",
    "3 metros",
    "1m",
    "2m",
    "3m",
    "gan",
    "carga rápida",
    "carga rapida",
    "fast charge",
    "fastcharge",
    "qc 3.0",
    "pd 20",
    "pd20",
    "pd 30",
)


# Categorías declaradas que ya marcan accesorio (cuando vienen del extractor).
_ACCESSORY_CATEGORIES: frozenset[str] = frozenset(
    {
        "phone_accessory",
        "charger",
        "cable",
        "generic_accessory",
        "case",
        "screen_protector",
    }
)


# ---------------------------------------------------------------------------
# "compatible con <marca>" / "para <marca>"
# ---------------------------------------------------------------------------


_COMPATIBLE_WITH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bcompatible[s]?\s+con\s+(iphone|ipad|airpods|apple\s+watch|samsung|"
        r"galaxy|huawei|xiaomi|redmi|motorola|sony|ps5|nintendo)\b",
        re.IGNORECASE,
    ),
    # "para iPhone 12/13/14", "para Samsung Galaxy", "para iPad mini"
    re.compile(
        r"\bpara\s+(iphone|ipad|airpods|apple\s+watch|samsung|galaxy|huawei|"
        r"xiaomi|redmi|motorola|nintendo|sony|ps5)\b",
        re.IGNORECASE,
    ),
    # Lista enumerada típica: "iPhone 16/15/14/13/12/11"
    re.compile(
        r"\biphone\s*\d+\s*[/\-,]\s*\d+",
        re.IGNORECASE,
    ),
)


# ---------------------------------------------------------------------------
# Producto premium real (lista cerrada, conservadora)
# ---------------------------------------------------------------------------

_REAL_PREMIUM_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Apple
    re.compile(r"\bapple\s+(iphone|ipad|airpods|watch|macbook|imac|mac\s+mini)\b", re.IGNORECASE),
    # iPhone real (sin "compatible con" cerca, lo verificamos arriba)
    re.compile(r"\biphone\s+(\d+\s*(pro|plus|mini|pro\s+max|max)?)\b", re.IGNORECASE),
    re.compile(r"\bipad\s+(pro|air|mini)?\b", re.IGNORECASE),
    re.compile(r"\bairpods\s+(pro|max)?\b", re.IGNORECASE),
    re.compile(r"\bapple\s+watch\b", re.IGNORECASE),
    re.compile(r"\bmacbook\s+(air|pro)?\b", re.IGNORECASE),
    # Samsung Galaxy flagship
    re.compile(r"\bgalaxy\s+(s\d+|note\s+\d+|z\s+(fold|flip)\s*\d*|tab\s+s\d+)\b", re.IGNORECASE),
    # Sony audio flagship
    re.compile(r"\bsony\s+(wf-?1000xm\d|wh-?1000xm\d)\b", re.IGNORECASE),
    re.compile(r"\bquietcomfort\s+ultra\b", re.IGNORECASE),
    # Laptops reales (con stack + chipset)
    re.compile(r"\b(dell|hp|lenovo|asus|msi|acer|gigabyte)\s+\w+", re.IGNORECASE),
    re.compile(r"\bthinkpad|elitebook|latitude|probook|vivobook|zenbook|legion|rog\b", re.IGNORECASE),
)


# ---------------------------------------------------------------------------
# Resultado tipado para auditar decisiones
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccessoryAssessment:
    """Resultado del análisis de un título.

    Campos:
        is_generic_accessory: True si el título describe un accesorio.
        mentions_compatible_with_premium: True si menciona "compatible con
            iPhone/Samsung/iPad", etc.
        is_real_premium_product: True si describe un producto real de marca
            premium reconocida (NO sólo accesorio compatible).
        category_guess: sugerencia de category_guess para el extractor /
            scorer (`charger`, `cable`, `case`, etc.).
        matched_tokens: tokens que dispararon la detección, en orden de
            aparición. Útil para reasons + auditoría.
    """

    is_generic_accessory: bool
    mentions_compatible_with_premium: bool
    is_real_premium_product: bool
    category_guess: Optional[str]
    matched_tokens: tuple[str, ...]


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def assess_title(
    title: Optional[str],
    *,
    category: Optional[str] = None,
    brand: Optional[str] = None,
) -> AccessoryAssessment:
    """Analiza un título y devuelve `AccessoryAssessment`.

    `category` y `brand` son hints opcionales del extractor; se usan para
    confirmar pero no son requeridos.
    """
    if not title:
        return AccessoryAssessment(
            is_generic_accessory=False,
            mentions_compatible_with_premium=False,
            is_real_premium_product=False,
            category_guess=None,
            matched_tokens=(),
        )

    norm = title.lower()
    matched: list[str] = []

    primary_hits: list[str] = []
    for tok, pat in zip(_ACCESSORY_TOKENS, _ACCESSORY_TOKEN_PATTERNS):
        if pat.search(title):
            primary_hits.append(tok)
    secondary_hits = [t for t in _ACCESSORY_SECONDARY_TOKENS if t in norm]

    matched.extend(primary_hits)
    matched.extend(secondary_hits)

    cat_norm = (category or "").lower().strip()
    is_accessory = bool(primary_hits) or cat_norm in _ACCESSORY_CATEGORIES

    # Excepción: productos audio premium reales (Sony WF-1000XM5,
    # AirPods Pro/Max, QuietComfort Ultra/45, Sennheiser Momentum) son
    # legítimamente "audífonos" en el título, pero NO son accesorios
    # genéricos. Si matchean patrón premium real, anulamos el flag de
    # accesorio para que conserven los bonus de error de precio.
    _PREMIUM_AUDIO_PATTERNS = (
        re.compile(r"\bsony\s+wf-?1000xm\d", re.IGNORECASE),
        re.compile(r"\bsony\s+wh-?1000xm\d", re.IGNORECASE),
        re.compile(r"\bairpods\s+(pro|max)", re.IGNORECASE),
        re.compile(r"\bquietcomfort\s+(ultra|45|earbuds)", re.IGNORECASE),
        re.compile(r"\bbose\s+qc\s*(ultra|45)", re.IGNORECASE),
        re.compile(r"\bsennheiser\s+momentum", re.IGNORECASE),
    )
    if is_accessory and any(p.search(title) for p in _PREMIUM_AUDIO_PATTERNS):
        # El título describe un producto premium audio reconocido.
        # Ej: "Audífonos Sony WF-1000XM5" - "audífonos" hace match pero
        # el modelo Sony WF-1000XM5 es flagship real.
        is_accessory = False
        # Removemos el match audio del set para no contaminar reasons.
        _AUDIO_TOKENS_TO_REMOVE = {
            "audífonos", "audifonos", "auriculares", "earbuds",
            "earphones", "headphones", "headset", "audífono",
            "audifono", "buds",
        }
        primary_hits = [
            h for h in primary_hits if h not in _AUDIO_TOKENS_TO_REMOVE
        ]
        matched = [
            m for m in matched if m not in _AUDIO_TOKENS_TO_REMOVE
        ]
        # Si tras quitar tokens audio queda algún token de accesorio
        # (cable, cargador, funda, etc.), reactivamos el flag.
        if primary_hits:
            is_accessory = True

    # Categoría sugerida.
    category_guess: Optional[str] = None
    if cat_norm in _ACCESSORY_CATEGORIES:
        category_guess = cat_norm
    elif "cargador" in norm or any(t in norm for t in ("20w", "30w", "65w", "100w")):
        category_guess = "charger"
    elif "cable" in norm and any(t in norm for t in ("tipo c", "type-c", "usb c", "lightning", "1m", "2m")):
        category_guess = "cable"
    elif any(t in norm for t in ("funda", "case", "carcasa")):
        category_guess = "case"
    elif any(t in norm for t in ("mica", "vidrio templado", "cristal templado", "protector de pantalla")):
        category_guess = "screen_protector"
    elif is_accessory:
        category_guess = "phone_accessory"

    # "compatible con <marca>" / "para <marca>"
    compatible_with_premium = any(p.search(title) for p in _COMPATIBLE_WITH_PATTERNS)
    if compatible_with_premium:
        # Captura textual mínima para reasons.
        for p in _COMPATIBLE_WITH_PATTERNS:
            m = p.search(title)
            if m:
                matched.append(m.group(0).strip().lower())
                break

    # Producto premium real (sólo si NO es accesorio).
    is_real_premium = False
    if not is_accessory:
        for p in _REAL_PREMIUM_PATTERNS:
            m = p.search(title)
            if m is None:
                continue
            # Si lo que capturamos es "iphone 14/13/12" porque es lista de
            # compatibilidad, no cuenta. La detección de compatibilidad ya
            # lo hizo arriba.
            if compatible_with_premium and m.group(0).lower().startswith("iphone"):
                continue
            is_real_premium = True
            break
        # Si la marca venida del extractor es premium y el título no
        # menciona "compatible con", lo aceptamos también.
        if not is_real_premium and brand and brand.lower() in {
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
            "lg",
            "huawei",
            "gigabyte",
        } and not compatible_with_premium and not is_accessory:
            is_real_premium = True

    return AccessoryAssessment(
        is_generic_accessory=is_accessory,
        mentions_compatible_with_premium=compatible_with_premium,
        is_real_premium_product=is_real_premium,
        category_guess=category_guess,
        # dedup conservando orden
        matched_tokens=tuple(dict.fromkeys(matched)),
    )


def is_generic_accessory(
    title: Optional[str],
    *,
    category: Optional[str] = None,
    brand: Optional[str] = None,
) -> bool:
    """Atajo: True si `title` describe un accesorio genérico."""
    return assess_title(title, category=category, brand=brand).is_generic_accessory


def mentions_compatible_with_premium(title: Optional[str]) -> bool:
    """Atajo: True si el título usa "compatible con / para <marca premium>".

    No depende de category ni brand.
    """
    return assess_title(title).mentions_compatible_with_premium


def is_real_premium_product(
    title: Optional[str],
    *,
    brand: Optional[str] = None,
) -> bool:
    """Atajo: True si el título es un producto premium real (no accesorio)."""
    return assess_title(title, brand=brand).is_real_premium_product
