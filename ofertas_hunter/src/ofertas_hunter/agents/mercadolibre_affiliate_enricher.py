"""Servicio para enriquecer items existentes del outbox con affiliate_url.

Caso de uso: el dispatcher tiene items ML antiguos (anteriores a Fase 3.4 o
pendientes desde antes de generarse el afiliado). Este servicio:

1. Selecciona items ML elegibles del outbox (marketplace=mercadolibre,
   source != 'telegram', sin affiliate_url).
2. Para cada uno, llama al `AffiliateExtractor` con `canonical_url`.
3. Si el extractor responde `success=True`, actualiza el payload con
   `affiliate_url, affiliate_product_id, commission_text` y deja
   `url=affiliate_url`. Marca `affiliate_status="ok"`.
4. Si falla, registra `affiliate_status="failed"` (o `"pending"` si el
   extractor no estaba disponible) y `affiliate_error=<motivo>`. **No
   publica** ni cambia el `state` del outbox: simplemente deja la nota
   en el payload para reintentos futuros.

Es la **única implementación** de la lógica enrichment: el comando
CLI y `scripts/enrich_affiliate_links.py` lo invocan.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..marketplaces.mercadolibre_affiliate import (
    AffiliateExtractor,
    AffiliateInfo,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resultado por item
# ---------------------------------------------------------------------------


@dataclass
class EnrichmentOutcome:
    outbox_id: int
    canonical_url: Optional[str]
    skipped_reason: Optional[str] = None  # telegram_source, has_affiliate, no_url, ...
    affiliate_url: Optional[str] = None
    affiliate_product_id: Optional[str] = None
    commission_text: Optional[str] = None
    error: Optional[str] = None
    status: str = "pending"  # ok | failed | skipped | pending


@dataclass
class EnrichmentReport:
    total_candidates: int = 0
    enriched: int = 0
    failed: int = 0
    skipped: int = 0
    outcomes: list[EnrichmentOutcome] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Servicio
# ---------------------------------------------------------------------------


class MercadoLibreAffiliateEnricher:
    """Servicio que enriquece outbox items ML con affiliate_url.

    Recibe la conexión SQLite y un `AffiliateExtractor`. Es síncrono salvo en
    la llamada al extractor (que es async).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        extractor: Optional[AffiliateExtractor],
    ) -> None:
        self.db = conn
        self.extractor = extractor

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    async def run(self, *, limit: int = 20) -> EnrichmentReport:
        """Procesa hasta `limit` items elegibles del outbox.

        Selecciona items pendientes ML que NO sean de Telegram y que NO tengan
        ya `affiliate_url`. Llama al extractor por cada uno y actualiza el
        payload con el resultado.
        """
        report = EnrichmentReport()
        candidates = self._select_candidates(limit=limit)
        report.total_candidates = len(candidates)

        if not candidates:
            logger.info("ml-enrich-affiliates: no hay candidatos")
            return report

        if self.extractor is None:
            logger.warning(
                "ml-enrich-affiliates: extractor no disponible; marco %d items como pending",
                len(candidates),
            )
            for outbox_id, payload in candidates:
                outcome = EnrichmentOutcome(
                    outbox_id=outbox_id,
                    canonical_url=payload.get("canonical_url"),
                    error="no_extractor",
                    status="pending",
                )
                self._persist_failure(outbox_id, payload, error="no_extractor", status="pending")
                report.outcomes.append(outcome)
                report.failed += 1
            return report

        for outbox_id, payload in candidates:
            outcome = await self._enrich_one(outbox_id, payload)
            report.outcomes.append(outcome)
            if outcome.status == "ok":
                report.enriched += 1
            elif outcome.status == "skipped":
                report.skipped += 1
            else:
                report.failed += 1
        return report

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    def _select_candidates(self, *, limit: int) -> list[tuple[int, dict]]:
        """SELECT items ML pendientes/in_flight sin affiliate_url y no-Telegram."""
        rows = self.db.execute(
            """
            SELECT id, message_payload_json
              FROM outbox
             WHERE state IN ('pending', 'in_flight')
               AND message_payload_json LIKE '%"marketplace":%mercadolibre%'
             ORDER BY id ASC
            """
        ).fetchall()

        candidates: list[tuple[int, dict]] = []
        for row in rows:
            try:
                payload = json.loads(row["message_payload_json"])
            except (json.JSONDecodeError, TypeError):
                continue

            marketplace = (payload.get("marketplace") or "").lower()
            if marketplace != "mercadolibre":
                continue
            source = (payload.get("source") or "").lower()
            if source == "telegram":
                continue
            if payload.get("affiliate_url"):
                continue

            candidates.append((row["id"], payload))
            if len(candidates) >= limit:
                break
        return candidates

    async def _enrich_one(self, outbox_id: int, payload: dict) -> EnrichmentOutcome:
        canonical_url = payload.get("canonical_url") or payload.get("url")
        if not canonical_url:
            self._persist_failure(outbox_id, payload, error="no_canonical_url", status="failed")
            return EnrichmentOutcome(
                outbox_id=outbox_id,
                canonical_url=None,
                error="no_canonical_url",
                status="failed",
            )

        try:
            info: AffiliateInfo = await self.extractor.extract(canonical_url)
        except Exception as exc:
            logger.warning("affiliate extractor raised for outbox=%s: %s", outbox_id, exc)
            self._persist_failure(
                outbox_id, payload, error=f"unexpected: {exc}", status="failed"
            )
            return EnrichmentOutcome(
                outbox_id=outbox_id,
                canonical_url=canonical_url,
                error=f"unexpected: {exc}",
                status="failed",
            )

        if not info.success or not info.affiliate_url:
            error = info.error or "unknown"
            self._persist_failure(outbox_id, payload, error=error, status="failed")
            return EnrichmentOutcome(
                outbox_id=outbox_id,
                canonical_url=canonical_url,
                error=error,
                status="failed",
            )

        self._persist_success(outbox_id, payload, info)
        return EnrichmentOutcome(
            outbox_id=outbox_id,
            canonical_url=canonical_url,
            affiliate_url=info.affiliate_url,
            affiliate_product_id=info.affiliate_product_id,
            commission_text=info.commission_text,
            status="ok",
        )

    # ------------------------------------------------------------------
    # Persistencia
    # ------------------------------------------------------------------

    def _persist_success(
        self, outbox_id: int, payload: dict, info: AffiliateInfo
    ) -> None:
        new_payload = dict(payload)
        new_payload["affiliate_url"] = info.affiliate_url
        new_payload["affiliate_product_id"] = info.affiliate_product_id
        new_payload["commission_text"] = info.commission_text
        new_payload["url"] = info.affiliate_url  # publish prefiere afiliado
        new_payload["affiliate_status"] = "ok"
        new_payload["affiliate_error"] = None
        new_payload["affiliate_enriched_at"] = _now_iso()
        # No tocamos canonical_url ni nada más.
        self._update_outbox_payload(outbox_id, new_payload)
        # Y reflejamos en `products` si se puede.
        self._update_product_affiliate(payload, info)

    def _persist_failure(
        self, outbox_id: int, payload: dict, *, error: str, status: str
    ) -> None:
        new_payload = dict(payload)
        new_payload["affiliate_status"] = status
        new_payload["affiliate_error"] = error
        new_payload["affiliate_enriched_at"] = _now_iso()
        # NO seteamos affiliate_url. Conservamos lo que había en `url`.
        self._update_outbox_payload(outbox_id, new_payload)

    def _update_outbox_payload(self, outbox_id: int, payload: dict) -> None:
        self.db.execute(
            "UPDATE outbox SET message_payload_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), outbox_id),
        )

    def _update_product_affiliate(self, payload: dict, info: AffiliateInfo) -> None:
        canonical = payload.get("canonical_url")
        if not canonical:
            return
        try:
            self.db.execute(
                "UPDATE products SET affiliate_link = COALESCE(?, affiliate_link), "
                "affiliate_product_id = COALESCE(?, affiliate_product_id), "
                "commission_text = COALESCE(?, commission_text) "
                "WHERE url_canonical = ?",
                (
                    info.affiliate_url,
                    info.affiliate_product_id,
                    info.commission_text,
                    canonical,
                ),
            )
        except sqlite3.Error as exc:
            logger.debug("update_product_affiliate failed: %s", exc)


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
