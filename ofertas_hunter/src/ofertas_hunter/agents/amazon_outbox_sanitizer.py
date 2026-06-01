"""Saneamiento del outbox Amazon.

Clasifica items Amazon `pending` y los bloquea (estado `discarded`) cuando
no cumplen las reglas duras de publicación. Devuelve razones granulares que
espejan exactamente las del gate de publicación (`WhatsAppPublisher._amazon_gate`)
para que el desglose de `reject_reason` sea consistente en todo el pipeline:

- `amazon_missing_affiliate`            → sin afiliado válido (`amzn.to/` o `tag=`).
- `amazon_no_verified_old_price`        → oferta normal sin precio anterior verificado.
- `amazon_current_price_unverified`     → precio actual no verificado.
- `amazon_unit_price_as_current_price`  → precio por unidad tomado como total.
- `amazon_extreme_discount_unverified`  → descuento >= umbral sin verificar.
- `amazon_current_price_suspicious`     → precio absoluto sospechoso / pack unitario.
- `amazon_telegram_needs_pdp_revalidation` → Amazon vía Telegram sin revalidación PDP.
- `skip_non_amazon`                     → no es Amazon (no se toca).
- `ok`                                  → cumple las reglas.

El comando CLI primero intenta enriquecer afiliados (reusando
`AmazonAffiliateEnricher`) y luego bloquea lo que siga sin cumplir.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..models import OutboxState


logger = logging.getLogger(__name__)


# Umbrales por defecto (espejo de config.py / publisher).
DEFAULT_EXTREME_DISCOUNT_THRESHOLD = 90.0
DEFAULT_MIN_ABSOLUTE_PRICE = 10.0
# Por encima de este precio, un título "de pack/unidad" no se considera
# sospechoso (el precio claramente no es unitario).
SUSPICIOUS_UNIT_PRICE_MAX = 20.0

# Razones granulares (espejo del gate de publicación).
R_SKIP = "skip_non_amazon"
R_OK = "ok"
R_MISSING_AFFILIATE = "amazon_missing_affiliate"
R_NO_VERIFIED_OLD_PRICE = "amazon_no_verified_old_price"
R_CURRENT_PRICE_UNVERIFIED = "amazon_current_price_unverified"
R_UNIT_PRICE = "amazon_unit_price_as_current_price"
R_EXTREME_DISCOUNT = "amazon_extreme_discount_unverified"
R_SUSPICIOUS = "amazon_current_price_suspicious"
R_TELEGRAM_NEEDS_PDP = "amazon_telegram_needs_pdp_revalidation"

# Razones que representan un bloqueo real (todo lo que no sea ok/skip).
BLOCK_REASONS = (
    R_MISSING_AFFILIATE,
    R_NO_VERIFIED_OLD_PRICE,
    R_CURRENT_PRICE_UNVERIFIED,
    R_UNIT_PRICE,
    R_EXTREME_DISCOUNT,
    R_SUSPICIOUS,
    R_TELEGRAM_NEEDS_PDP,
)


def _is_valid_affiliate_url(url: Optional[str]) -> bool:
    if not url or not isinstance(url, str):
        return False
    return ("amzn.to/" in url) or ("tag=" in url)


_UNIT_PRICE_TEXT_RE = re.compile(
    r"(/|\bpor\b)\s*(unidad(?:es)?|unid\.?|pieza(?:s)?|pza\.?|pz\.?|count|each|recuento)\b",
    re.IGNORECASE,
)

# Texto adicional de precio-por-unidad usado por el usuario en sus reglas.
_UNIT_PRICE_EXTRA_RE = re.compile(
    r"(/\s*unidad|por\s+unidad|/\s*pieza|por\s+pieza|\bunit\b|\beach\b|\bppu\b)",
    re.IGNORECASE,
)

# Palabras clave de paquetes/unidades (falsos positivos típicos de guantes, etc.).
_PACK_KEYWORDS = (
    "guante",
    "nitrilo",
    "vinil",
    "latex",
    "látex",
    "desechable",
    "pieza",
    "piezas",
    "pack",
    "unidad",
    "unidades",
)


def _is_unit_price_text(raw_text: str) -> bool:
    return bool(_UNIT_PRICE_TEXT_RE.search(raw_text) or _UNIT_PRICE_EXTRA_RE.search(raw_text))


def _is_pack_title(title: str) -> bool:
    low = title.lower()
    return any(kw in low for kw in _PACK_KEYWORDS)


def _from_telegram(payload: dict) -> bool:
    src = str(payload.get("source") or "").lower()
    ext = str(payload.get("extractor") or "").lower()
    return src == "telegram" or ext == "telegram"


def classify_amazon_pending(
    payload: dict,
    *,
    item_type: str,
    extreme_threshold: float = DEFAULT_EXTREME_DISCOUNT_THRESHOLD,
    min_absolute_price: float = DEFAULT_MIN_ABSOLUTE_PRICE,
) -> str:
    """Clasifica un item Amazon del outbox devolviendo una razón granular.

    Ver el docstring del módulo para la lista de razones. El orden de las
    comprobaciones espeja `WhatsAppPublisher._amazon_gate` salvo por los
    chequeos de verificación de precio actual y de Telegram, que son
    específicos del saneamiento.
    """
    if (payload.get("marketplace") or "").lower() != "amazon":
        return R_SKIP

    # 1. Afiliado válido.
    if not _is_valid_affiliate_url(payload.get("affiliate_url")):
        return R_MISSING_AFFILIATE

    # 2. Amazon vía Telegram: requiere revalidación de PDP. Si no tiene ambos
    #    precios verificados, se mantiene bloqueado (no inventamos precio).
    if _from_telegram(payload):
        if not (payload.get("old_price_verified") and payload.get("current_price_verified")):
            return R_TELEGRAM_NEEDS_PDP

    # 3. Ofertas normales: precio anterior + actual verificados, sin extremos.
    if item_type == "normal":
        if not payload.get("old_price_verified"):
            return R_NO_VERIFIED_OLD_PRICE

        prev_raw = payload.get("previous_price")
        if prev_raw is None:
            prev_raw = payload.get("old_price")
        try:
            cur = float(payload.get("current_price"))
            prev = float(prev_raw)
        except (TypeError, ValueError):
            return R_NO_VERIFIED_OLD_PRICE
        if prev <= cur:
            return R_NO_VERIFIED_OLD_PRICE

        # 3b. Precio actual verificado.
        if not payload.get("current_price_verified"):
            return R_CURRENT_PRICE_UNVERIFIED

        # 3c. Precio por unidad tomado como total.
        raw_text = str(payload.get("current_price_raw_text") or "")
        if _is_unit_price_text(raw_text) or payload.get("current_price_is_unit_price"):
            return R_UNIT_PRICE

        extreme_verified = bool(payload.get("extreme_discount_verified"))
        computed = round((prev - cur) / prev * 100) if prev > 0 else 0

        # 3d. Descuento extremo no verificado.
        if not extreme_verified:
            declared = payload.get("discount_percent")
            declared_extreme = False
            try:
                declared_extreme = declared is not None and float(declared) >= extreme_threshold
            except (TypeError, ValueError):
                declared_extreme = False
            if computed >= extreme_threshold or declared_extreme:
                return R_EXTREME_DISCOUNT

            # 3e. Precio absoluto sospechoso (muy bajo frente a old alto).
            if cur < min_absolute_price and prev > 50:
                return R_SUSPICIOUS

            # 3f. current_price < 1% del old_price → casi siempre unit price.
            if prev > 0 and cur < (prev * 0.01):
                return R_SUSPICIOUS

        # 3g. Título de pack/unidad con precio que parece unitario.
        title = str(payload.get("title") or "")
        if _is_pack_title(title) and cur < SUSPICIOUS_UNIT_PRICE_MAX and not extreme_verified:
            return R_SUSPICIOUS

    return R_OK


@dataclass
class SanitizeReport:
    total: int = 0
    already_ok: int = 0
    enriched: int = 0
    enrich_failed: int = 0
    dry_run: bool = False
    blocked_by_reason: dict[str, int] = field(default_factory=dict)

    # --- Conveniencias retro-compatibles -------------------------------
    @property
    def blocked_total(self) -> int:
        return sum(self.blocked_by_reason.values())

    @property
    def blocked_affiliate(self) -> int:
        return self.blocked_by_reason.get(R_MISSING_AFFILIATE, 0)

    @property
    def blocked_old_price(self) -> int:
        return self.blocked_by_reason.get(R_NO_VERIFIED_OLD_PRICE, 0)

    @property
    def blocked_suspicious(self) -> int:
        return (
            self.blocked_by_reason.get(R_SUSPICIOUS, 0)
            + self.blocked_by_reason.get(R_UNIT_PRICE, 0)
            + self.blocked_by_reason.get(R_EXTREME_DISCOUNT, 0)
        )

    def _bump(self, reason: str) -> None:
        self.blocked_by_reason[reason] = self.blocked_by_reason.get(reason, 0) + 1

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "already_ok": self.already_ok,
            "enriched": self.enriched,
            "enrich_failed": self.enrich_failed,
            "dry_run": self.dry_run,
            "blocked_total": self.blocked_total,
            "blocked_by_reason": dict(self.blocked_by_reason),
        }


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _block(conn: sqlite3.Connection, outbox_id: int, payload: dict, reason: str) -> None:
    new_payload = dict(payload)
    new_payload["reject_reason"] = reason
    conn.execute(
        "UPDATE outbox SET state = ?, message_payload_json = ? WHERE id = ?",
        (OutboxState.DISCARDED.value, json.dumps(new_payload, ensure_ascii=False), outbox_id),
    )


async def sanitize_amazon_outbox(
    conn: sqlite3.Connection,
    *,
    enricher=None,
    enrich_limit: int = 20,
    dry_run: bool = False,
) -> SanitizeReport:
    """Sanea el outbox Amazon: enriquece afiliados y bloquea lo no publicable.

    `enricher` es opcional (`AmazonAffiliateEnricher`); si se provee, se corre
    primero para resolver afiliados faltantes. Con `dry_run=True` se clasifica
    y se cuenta todo, pero no se modifica ninguna fila del outbox.
    """
    report = SanitizeReport(dry_run=dry_run)

    # 1. Enriquecer afiliados faltantes (si hay enricher y no es dry-run).
    if enricher is not None and not dry_run:
        try:
            enr = await enricher.run(limit=enrich_limit)
            report.enriched = getattr(enr, "enriched", 0)
            report.enrich_failed = getattr(enr, "failed", 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sanitize: enricher falló: %s", exc)

    # 2. Reclasificar y bloquear lo que siga sin cumplir.
    rows = conn.execute(
        "SELECT id, type, message_payload_json FROM outbox WHERE state = ?",
        (OutboxState.PENDING.value,),
    ).fetchall()

    for row in rows:
        outbox_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]
        item_type = row["type"] if isinstance(row, sqlite3.Row) else row[1]
        raw = row["message_payload_json"] if isinstance(row, sqlite3.Row) else row[2]
        try:
            payload = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            payload = {}

        verdict = classify_amazon_pending(payload, item_type=item_type)
        if verdict == R_SKIP:
            continue
        report.total += 1
        if verdict == R_OK:
            report.already_ok += 1
            continue
        if verdict in BLOCK_REASONS:
            if not dry_run:
                _block(conn, outbox_id, payload, verdict)
            report._bump(verdict)  # noqa: SLF001

    if not dry_run:
        conn.commit()
    return report
