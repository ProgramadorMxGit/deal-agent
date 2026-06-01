"""Modelos de datos (dataclasses) usados en todo el proyecto.

Estos son objetos puros de transporte/dominio. La persistencia vive en
`repositories/`. Se mantienen separados para que los tests unitarios sean
deterministas y no toquen DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Marketplace(str, Enum):
    AMAZON = "amazon"
    MERCADOLIBRE = "mercadolibre"
    TELEGRAM = "telegram"
    WALMART = "walmart"
    SAMS_CLUB = "sams_club"
    COPPEL = "coppel"
    OFFICE_DEPOT = "office_depot"
    LIVERPOOL = "liverpool"
    SEARS = "sears"
    DELL = "dell"
    SONY_STORE = "sony_store"
    COSTCO = "costco"
    BESTBUY = "bestbuy"
    OTHER = "other"


class Source(str, Enum):
    AMAZON_HUNTER = "amazon_hunter"
    MERCADOLIBRE_HUNTER = "mercadolibre_hunter"
    TELEGRAM = "telegram"
    MANUAL = "manual"


class Condition(str, Enum):
    NEW = "new"
    USED = "used"
    REFURBISHED = "refurbished"
    UNKNOWN = "unknown"


class Classification(str, Enum):
    PRICE_ERROR_CONFIRMED = "price_error_confirmed"
    POSSIBLE_PRICE_ERROR = "possible_price_error"
    SUSPICIOUS_DEAL = "suspicious_deal"
    NORMAL_OFFER = "normal_offer"
    NO_PRICE_ERROR = "no_price_error"


class OfferState(str, Enum):
    CANDIDATE = "candidate"
    ELIGIBLE = "eligible"
    PUBLISHED = "published"
    DISCARDED = "discarded"
    EXPIRED = "expired"
    WATCHLIST = "watchlist"


class OutboxType(str, Enum):
    NORMAL = "normal"
    PRICE_ERROR = "price_error"
    POSSIBLE_PE = "possible_pe"


class OutboxState(str, Enum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    SENT = "sent"
    FAILED = "failed"
    DISCARDED = "discarded"
    DEFERRED = "deferred"


# ---------------------------------------------------------------------------
# Dominio
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Product:
    marketplace: str
    url_canonical: str
    title: str
    marketplace_id: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    category_inferred: bool = False
    condition: str = Condition.NEW.value
    image_url: Optional[str] = None
    affiliate_link: Optional[str] = None
    affiliate_product_id: Optional[str] = None
    commission_text: Optional[str] = None
    first_seen_at: datetime = field(default_factory=_utcnow)
    last_seen_at: datetime = field(default_factory=_utcnow)
    id: Optional[int] = None


@dataclass
class PriceObservation:
    product_id: int
    source: str
    current_price: Optional[float] = None
    previous_price: Optional[float] = None
    currency: str = "MXN"
    discount_percent: Optional[float] = None
    has_stock: Optional[bool] = None
    raw_signals: dict = field(default_factory=dict)
    observed_at: datetime = field(default_factory=_utcnow)
    id: Optional[int] = None


@dataclass
class Offer:
    product_id: int
    classification: str
    score: int
    reasons: list[str]
    state: str
    discount_percent: Optional[float] = None
    current_price_observation_id: Optional[int] = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    id: Optional[int] = None


@dataclass
class OutboxItem:
    offer_id: int
    type: str
    message_payload: dict
    enqueued_at: datetime = field(default_factory=_utcnow)
    scheduled_for: Optional[datetime] = None
    attempts: int = 0
    last_attempt_at: Optional[datetime] = None
    state: str = OutboxState.PENDING.value
    id: Optional[int] = None


@dataclass
class PublishedMessage:
    sent_at: datetime
    success: bool
    message_text: str
    outbox_id: Optional[int] = None
    offer_id: Optional[int] = None
    evolution_response: Optional[str] = None
    media_url: Optional[str] = None
    id: Optional[int] = None


# ---------------------------------------------------------------------------
# PriceErrorSignal — input central del scorer
# ---------------------------------------------------------------------------


@dataclass
class PriceErrorSignal:
    """Conjunto completo de señales para evaluar un posible error de precio.

    Cualquier campo desconocido se deja en `None` o vacío. El scorer usa lo
    que tenga.
    """

    product_title: str
    marketplace: str
    source: str
    original_url: str

    source_channel: Optional[str] = None
    original_text: Optional[str] = None
    resolved_url: Optional[str] = None

    current_price: Optional[float] = None
    previous_price: Optional[float] = None
    historical_median_price: Optional[float] = None
    category_estimated_range_min: Optional[float] = None
    category_estimated_range_max: Optional[float] = None
    discount_percent: Optional[float] = None

    brand: Optional[str] = None
    category: Optional[str] = None
    condition: str = Condition.NEW.value

    has_stock: Optional[bool] = None
    has_image: bool = False

    urgency_terms: list[str] = field(default_factory=list)
    telegram_confidence: float = 0.0

    # Subscores poblados por el scorer
    price_anomaly_score: int = 0
    category_anomaly_score: int = 0
    historical_anomaly_score: int = 0
    final_score: int = 0
    classification: str = Classification.NO_PRICE_ERROR.value
    reasons: list[str] = field(default_factory=list)

    # Flags de descarte (no publicar)
    is_publishable: bool = True
    not_publishable_reasons: list[str] = field(default_factory=list)

    # Heurísticas adicionales (set explícitamente cuando el extractor lo detecta)
    monthly_payment_suspected: bool = False
    accessory_price_suspected: bool = False
    variant_mismatch: bool = False
    coupon_dependent: bool = False
    external_seller_suspect: bool = False
    generic_brand: bool = False

    created_at: datetime = field(default_factory=_utcnow)
    revalidated_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Resultado del scorer (legible, sin mutar el signal original)
# ---------------------------------------------------------------------------


@dataclass
class ScoringResult:
    score: int
    classification: str
    confidence_label: str
    reasons: list[str]
    is_publishable: bool
    not_publishable_reasons: list[str]
