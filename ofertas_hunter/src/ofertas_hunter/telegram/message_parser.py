"""Parser de mensajes de Telegram.

Convierte el texto crudo de un mensaje (sin contexto Telethon) en un
`ParsedTelegramMessage` con los campos que necesita `price_intelligence`.

La función principal es `parse_message(text, channel, message_id, captured_at,
image_path=None)`.

Las reglas duras viven aquí:

- Si el link es de Mercado Libre, se marca `skip_reason='mercadolibre_link'`
  (el listener descartará el mensaje sin enviarlo a `price_intelligence`).
- Si no se detecta tienda, se asigna `marketplace='other'`.
- Si no se detecta link, `skip_reason='no_link'`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .signal_extractor import UrgencySignals, extract_urgency


# --- Configuración ----------------------------------------------------------


_STORE_KEYWORDS: dict[str, list[str]] = {
    "amazon": ["amazon", "amzn"],
    "mercadolibre": ["mercado libre", "mercadolibre", "meli "],
    "walmart": ["walmart"],
    "sams_club": ["sam's club", "sams club", "sam´s club", "sams "],
    "coppel": ["coppel"],
    "office_depot": ["office depot"],
    "liverpool": ["liverpool"],
    "sears": ["sears"],
    "dell": ["dell"],
    "sony_store": ["sony store"],
    "costco": ["costco"],
    "bestbuy": ["bestbuy", "best buy"],
}

_BRAND_KEYWORDS: list[str] = [
    "apple",
    "samsung",
    "sony",
    "dell",
    "hp",
    "lenovo",
    "asus",
    "msi",
    "lg",
    "huawei",
    "xiaomi",
    "motorola",
    "nintendo",
    "microsoft",
    "amazon",
    "bose",
    "jbl",
    "acer",
    "gigabyte",
    "corsair",
    "kingston",
    "seagate",
    "wd",
]

_CATEGORY_HINTS: list[tuple[str, list[str]]] = [
    # Más específico primero
    (
        "smartphone_flagship",
        [
            "iphone 15",
            "iphone 16",
            "iphone pro",
            "iphone pro max",
            "galaxy s24",
            "galaxy s25",
            "galaxy s ultra",
            "galaxy z",
            "pixel 8",
            "pixel 9",
        ],
    ),
    (
        "laptop",
        ["laptop", "notebook", "macbook", "thinkpad", "elitebook", "vivobook", "ideapad"],
    ),
    (
        "smartphone",
        ["iphone", "galaxy", "smartphone", "celular", "redmi", "motorola", "pixel"],
    ),
    ("tablet", ["ipad", "tablet", "lenovo tab", "samsung tab"]),
    (
        "audio_premium",
        ["airpods", "wf-1000xm", "wf 1000xm", "bose qc", "sennheiser momentum", "audífonos", "headphones"],
    ),
    ("display", ["monitor", "smart tv", "pantalla"]),
    ("console", ["playstation", "ps5", "xbox", "nintendo", "consola"]),
    (
        "pc_components",
        ["motherboard", "gpu", "rtx", "ryzen", "intel core", "ssd", "ram"],
    ),
    ("home_appliance", ["lavadora", "refrigerador", "licuadora", "freidora"]),
    ("apparel", ["camisa", "playera", "tenis", "zapatos", "ropa"]),
]


_MERCADOLIBRE_HOSTS = (
    "mercadolibre.com.mx",
    "mercadolibre.com",
    "meli.la",
    "articulo.mercadolibre.com.mx",
    "listado.mercadolibre.com.mx",
)


_LINK_RE = re.compile(
    r"(?:https?://[^\s\)\]<>]+)|"
    r"(?:bit\.ly/[A-Za-z0-9_\-]+)|"
    r"(?:goo\.gl/[A-Za-z0-9_\-]+)|"
    r"(?:tinyurl\.com/[A-Za-z0-9_\-]+)|"
    r"(?:t\.co/[A-Za-z0-9_\-]+)|"
    r"(?:meli\.la/[A-Za-z0-9_\-]+)|"
    r"(?:amzn\.to/[A-Za-z0-9_\-]+)|"
    r"(?:amzn\.mx/[A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)

_PRICE_RE = re.compile(
    r"\$\s*([0-9]{1,3}(?:[,\.][0-9]{3})*(?:[\.,][0-9]{1,2})?|[0-9]+)(?!\s*%)",
)

_DISCOUNT_RE = re.compile(r"(\d{1,3})\s*%")

_HASHTAG_RE = re.compile(r"#([A-Za-zÁÉÍÓÚÑáéíóúñ0-9_]+)")


# --- Modelo -----------------------------------------------------------------


@dataclass
class ParsedTelegramMessage:
    channel: str
    message_id: int
    captured_at: datetime
    text: str
    title_guess: Optional[str] = None
    marketplace: Optional[str] = None
    store_mention: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    written_price: Optional[float] = None
    discount_visible: Optional[float] = None
    urgency_terms: list[str] = field(default_factory=list)
    urgency_score: int = 0
    is_price_error_keyword: bool = False
    original_url: Optional[str] = None
    original_urls: list[str] = field(default_factory=list)
    resolved_url: Optional[str] = None
    image_path: Optional[str] = None
    skip_reason: Optional[str] = None
    hashtags: list[str] = field(default_factory=list)
    chat_id: Optional[int] = None
    has_image: bool = False


# --- API --------------------------------------------------------------------


def parse_message(
    text: str,
    channel: str,
    message_id: int,
    captured_at: datetime,
    image_path: Optional[str] = None,
    *,
    chat_id: Optional[int] = None,
) -> ParsedTelegramMessage:
    """Parsea un mensaje de Telegram a campos estructurados.

    No accede a la red: `resolved_url` queda en None y debe ser resuelto
    posteriormente por `extraction.url_resolver`.
    """
    msg_text = text or ""

    # Extracción de partes
    urgency = extract_urgency(msg_text)

    title_guess = _extract_title(msg_text)
    written_price = _extract_price(msg_text)
    discount_visible = _extract_discount_visible(msg_text)
    store_mention, marketplace = _detect_store(msg_text)
    brand = _detect_brand(msg_text)
    category = _detect_category(msg_text)
    urls = _extract_all_urls(msg_text)
    original_url = urls[0] if urls else None
    hashtags = _extract_hashtags(msg_text)

    skip_reason: Optional[str] = None
    if marketplace == "mercadolibre" or _is_mercadolibre_link(original_url):
        skip_reason = "mercadolibre_link"
        marketplace = "mercadolibre"

    if not original_url and skip_reason is None:
        # Sin link no se puede validar; el listener decide si descartar o
        # guardar como observación.
        skip_reason = "no_link"

    return ParsedTelegramMessage(
        channel=channel,
        message_id=message_id,
        captured_at=captured_at,
        text=msg_text,
        title_guess=title_guess,
        marketplace=marketplace,
        store_mention=store_mention,
        brand=brand,
        category=category,
        written_price=written_price,
        discount_visible=discount_visible,
        urgency_terms=urgency.terms,
        urgency_score=urgency.score,
        is_price_error_keyword=urgency.has_price_error_keyword,
        original_url=original_url,
        original_urls=urls,
        resolved_url=None,
        image_path=image_path,
        skip_reason=skip_reason,
        hashtags=hashtags,
        chat_id=chat_id,
        has_image=bool(image_path),
    )


# --- Helpers privados -------------------------------------------------------


def _is_mercadolibre_link(url: Optional[str]) -> bool:
    if not url:
        return False
    lowered = url.lower()
    return any(host in lowered for host in _MERCADOLIBRE_HOSTS)


def _extract_title(text: str) -> Optional[str]:
    """La heurística simple es: primera línea no vacía sin URL ni emojis."""
    for raw_line in text.splitlines():
        candidate = raw_line.strip()
        if not candidate:
            continue
        # Quitar emojis al inicio
        candidate = re.sub(r"^[^\w\$\(]+", "", candidate)
        if not candidate:
            continue
        if _LINK_RE.search(candidate):
            continue
        if len(candidate) < 4:
            continue
        return candidate
    return None


def _extract_price(text: str) -> Optional[float]:
    if not text:
        return None
    candidates: list[float] = []
    for match in _PRICE_RE.finditer(text):
        raw = match.group(1)
        # Saltar si dentro de "X% de" — el regex ya excluye `%`
        try:
            candidates.append(_parse_price(raw))
        except ValueError:
            continue
    if not candidates:
        return None
    # El primer precio del mensaje suele ser el principal.
    return candidates[0]


def _parse_price(raw: str) -> float:
    cleaned = raw.strip()
    has_comma = "," in cleaned
    has_dot = "." in cleaned
    if has_comma and has_dot:
        # Decidir formato europeo vs americano
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif has_comma and not has_dot:
        parts = cleaned.split(",")
        if len(parts[-1]) == 2:
            cleaned = cleaned.replace(",", ".")  # decimal
        else:
            cleaned = cleaned.replace(",", "")  # miles
    return float(cleaned)


def _extract_discount_visible(text: str) -> Optional[float]:
    match = _DISCOUNT_RE.search(text)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    if 1 <= value <= 99:
        return float(value)
    return None


def _detect_store(text: str) -> tuple[Optional[str], Optional[str]]:
    lowered = text.lower()
    for marketplace, keywords in _STORE_KEYWORDS.items():
        for kw in keywords:
            if kw in lowered:
                return kw, marketplace
    return None, None


def _detect_brand(text: str) -> Optional[str]:
    lowered = text.lower()
    for brand in _BRAND_KEYWORDS:
        if re.search(rf"\b{re.escape(brand)}\b", lowered):
            return brand
    return None


def _detect_category(text: str) -> Optional[str]:
    lowered = text.lower()
    for cat, kws in _CATEGORY_HINTS:
        for kw in kws:
            if kw in lowered:
                return cat
    return None


def _extract_first_url(text: str) -> Optional[str]:
    urls = _extract_all_urls(text)
    return urls[0] if urls else None


def _extract_all_urls(text: str) -> list[str]:
    """Lista de URLs únicas en orden de aparición. Normaliza shortlinks sin scheme."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _LINK_RE.finditer(text):
        url = match.group(0).strip()
        if not url.lower().startswith("http"):
            url = "https://" + url
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _extract_hashtags(text: str) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _HASHTAG_RE.finditer(text):
        tag = match.group(1).lower()
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out
