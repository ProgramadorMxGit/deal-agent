"""Topes duros (hard caps) de diversidad para el selector del outbox.

Aplica restricciones DURAS sobre los candidatos elegibles ANTES de elegir,
usando la metadata normalizada (`category_normalized`, `brand_normalized`,
`product_family`, `title_fingerprint`) y el historial reciente de
publicaciones exitosas.

NO toca gates de publicación (afiliado, old_price, extreme_discount, etc.).
Un candidato que llega aquí YA pasó todos los gates de seguridad. Estos
filtros solo deciden el *orden/elegibilidad por diversidad*, nunca relajan
seguridad.

Política de override:
- Si TODOS los candidatos válidos violan diversidad y
  `allow_override_if_no_alternative=True`, se permite publicar el "menos
  malo" registrando `diversity_override_no_alternative`.
- Si `False`, no se publica en ese ciclo (el item sigue pending).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from ..models import OutboxItem
from .diversity_metadata import title_similarity


# Razones de rechazo por diversidad (NO marcan discarded; son skip temporal).
REASON_CATEGORY_WINDOW = "diversity_same_category_window"
REASON_BRAND_WINDOW = "diversity_same_brand_window"
REASON_FAMILY_WINDOW = "diversity_same_product_family"
REASON_MARKETPLACE_WINDOW = "diversity_same_marketplace_window"
REASON_FUZZY_TITLE = "diversity_duplicate_fuzzy_title"
REASON_FAMILY_RECENT = "diversity_same_product_family_recent"
REASON_DUPLICATE_EXACT = "diversity_duplicate_exact"


@dataclass(frozen=True)
class HistoryItem:
    """Entrada de historial ya normalizada (más reciente primero)."""

    marketplace: str
    category: Optional[str]
    brand: Optional[str]
    product_family: Optional[str]
    title_fingerprint: str
    item_id: Optional[str]
    sent_at: datetime


@dataclass
class CandidateMeta:
    """Metadata normalizada de un candidato (adjunta al OutboxItem)."""

    item: OutboxItem
    marketplace: str
    category: Optional[str]
    brand: Optional[str]
    product_family: Optional[str]
    title_fingerprint: str
    title: str
    item_id: Optional[str]


@dataclass
class Rejection:
    outbox_id: Optional[int]
    title: str
    reason: str
    category: Optional[str]
    brand: Optional[str]
    product_family: Optional[str]


@dataclass
class HardCapConfig:
    enabled: bool = True
    window_size: int = 10
    max_same_category: int = 3
    max_same_brand: int = 2
    max_same_product_family: int = 1
    max_same_marketplace: int = 7
    fuzzy_title_threshold: float = 0.85
    reject_similar_hours: int = 24
    allow_override_if_no_alternative: bool = True


@dataclass
class HardCapResult:
    kept: list[CandidateMeta] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    window_stats: dict = field(default_factory=dict)


def _window_counts(history: list[HistoryItem], window_size: int) -> dict:
    win = history[:window_size]
    cat: dict = {}
    brand: dict = {}
    mkt: dict = {}
    fam: dict = {}
    for h in win:
        if h.category:
            cat[h.category] = cat.get(h.category, 0) + 1
        if h.brand:
            brand[h.brand] = brand.get(h.brand, 0) + 1
        if h.marketplace:
            mkt[h.marketplace] = mkt.get(h.marketplace, 0) + 1
        if h.product_family:
            fam[h.product_family] = fam.get(h.product_family, 0) + 1
    return {
        "category_counts": cat,
        "brand_counts": brand,
        "marketplace_counts": mkt,
        "family_counts": fam,
    }


def apply_hard_caps(
    candidates: list[CandidateMeta],
    history: list[HistoryItem],
    config: HardCapConfig,
    now: datetime,
    *,
    has_marketplace_alternative: Optional[bool] = None,
) -> HardCapResult:
    """Filtra candidatos que violan los topes duros de diversidad.

    `candidates` deben venir YA validados por gates. `history` ordenado de
    más reciente a más antiguo. Devuelve los que sobreviven + razones de
    rechazo. NO decide override aquí (eso es responsabilidad del curator),
    solo reporta qué pasa y qué no.
    """
    stats = _window_counts(history, config.window_size)
    if not config.enabled:
        return HardCapResult(kept=list(candidates), rejected=[], window_stats=stats)

    win = history[: config.window_size]
    cat_counts = dict(stats["category_counts"])
    brand_counts = dict(stats["brand_counts"])
    mkt_counts = dict(stats["marketplace_counts"])
    fam_counts = dict(stats["family_counts"])

    # ¿hay candidatos de más de un marketplace? (para el cap de marketplace)
    candidate_marketplaces = {c.marketplace for c in candidates if c.marketplace}
    multiple_marketplaces_available = (
        has_marketplace_alternative
        if has_marketplace_alternative is not None
        else len(candidate_marketplaces) > 1
    )

    # ventana temporal para fuzzy/family recientes
    cutoff = now - timedelta(hours=config.reject_similar_hours)
    recent = [h for h in history if h.sent_at >= cutoff]

    kept: list[CandidateMeta] = []
    rejected: list[Rejection] = []

    for c in candidates:
        reason: Optional[str] = None

        # 1. Duplicado exacto por item_id en ventana temporal
        if c.item_id and any(h.item_id == c.item_id for h in recent):
            reason = REASON_DUPLICATE_EXACT

        # 2. Familia de producto repetida dentro de la ventana de publicaciones
        if reason is None and c.product_family:
            if fam_counts.get(c.product_family, 0) >= config.max_same_product_family:
                reason = REASON_FAMILY_WINDOW

        # 3. Familia repetida en ventana temporal reciente (24h)
        if reason is None and c.product_family:
            if any(h.product_family == c.product_family for h in recent):
                reason = REASON_FAMILY_RECENT

        # 4. Título fuzzy >= umbral con publicación reciente (24h).
        # Se exige un fingerprint con suficiente contenido (>=3 tokens) para
        # evitar falsos positivos por títulos triviales/cortos.
        if reason is None and c.title_fingerprint and len(c.title_fingerprint.split()) >= 3:
            for h in recent:
                ht = _hist_title(h)
                if len(ht.split()) < 3:
                    continue
                if title_similarity(c.title, ht) >= config.fuzzy_title_threshold:
                    reason = REASON_FUZZY_TITLE
                    break

        # 5. Categoría sobre el tope en ventana
        if reason is None and c.category:
            if cat_counts.get(c.category, 0) >= config.max_same_category:
                reason = REASON_CATEGORY_WINDOW

        # 6. Marca sobre el tope en ventana
        if reason is None and c.brand:
            if brand_counts.get(c.brand, 0) >= config.max_same_brand:
                reason = REASON_BRAND_WINDOW

        # 7. Marketplace sobre el tope (solo si hay alternativa de otro mkt)
        if reason is None and c.marketplace and multiple_marketplaces_available:
            if mkt_counts.get(c.marketplace, 0) >= config.max_same_marketplace:
                reason = REASON_MARKETPLACE_WINDOW

        if reason is None:
            kept.append(c)
        else:
            rejected.append(
                Rejection(
                    outbox_id=c.item.id,
                    title=c.title[:120],
                    reason=reason,
                    category=c.category,
                    brand=c.brand,
                    product_family=c.product_family,
                )
            )

    return HardCapResult(kept=kept, rejected=rejected, window_stats=stats)


def _hist_title(h: HistoryItem) -> str:
    # el historial guarda fingerprint; lo usamos como proxy del título
    return h.title_fingerprint


def least_repetitive(
    candidates: list[CandidateMeta],
    history: list[HistoryItem],
    config: HardCapConfig,
) -> Optional[CandidateMeta]:
    """Elige el candidato MENOS repetitivo (para override sin alternativa).

    Penaliza por cuántas veces su categoría/marca/familia aparecen en la
    ventana. Determinístico: en empate, el de menor outbox_id.
    """
    if not candidates:
        return None
    stats = _window_counts(history, config.window_size)
    cat_counts = stats["category_counts"]
    brand_counts = stats["brand_counts"]
    fam_counts = stats["family_counts"]

    def penalty(c: CandidateMeta) -> tuple:
        fam_p = fam_counts.get(c.product_family, 0) if c.product_family else 0
        brand_p = brand_counts.get(c.brand, 0) if c.brand else 0
        cat_p = cat_counts.get(c.category, 0) if c.category else 0
        # familia pesa más, luego marca, luego categoría; desempate por id
        return (fam_p, brand_p, cat_p, c.item.id or 0)

    return min(candidates, key=penalty)


__all__ = [
    "HardCapConfig",
    "HardCapResult",
    "HistoryItem",
    "CandidateMeta",
    "Rejection",
    "apply_hard_caps",
    "least_repetitive",
    "REASON_CATEGORY_WINDOW",
    "REASON_BRAND_WINDOW",
    "REASON_FAMILY_WINDOW",
    "REASON_MARKETPLACE_WINDOW",
    "REASON_FUZZY_TITLE",
    "REASON_FAMILY_RECENT",
    "REASON_DUPLICATE_EXACT",
]
