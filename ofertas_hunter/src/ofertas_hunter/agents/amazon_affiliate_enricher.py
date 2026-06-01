"""Servicio para enriquecer items Amazon existentes del outbox con affiliate_url."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..marketplaces.amazon_affiliate import (
    AffiliateExtractor,
    AffiliateInfo,
)


logger = logging.getLogger(__name__)


@dataclass
class EnrichmentOutcome:
    outbox_id: int
    canonical_url: Optional[str]
    affiliate_url: Optional[str] = None
    error: Optional[str] = None
    status: str = "pending"


@dataclass
class EnrichmentReport:
    total_candidates: int = 0
    enriched: int = 0
    failed: int = 0
    skipped: int = 0
    outcomes: list[EnrichmentOutcome] = field(default_factory=list)


class AmazonAffiliateEnricher:
    def __init__(
        self,
        conn: sqlite3.Connection,
        extractor: Optional[AffiliateExtractor],
    ) -> None:
        self.db = conn
        self.extractor = extractor

    async def run(self, *, limit: int = 20) -> EnrichmentReport:
        report = EnrichmentReport()
        candidates = self._select_candidates(limit=limit)
        report.total_candidates = len(candidates)

        if not candidates:
            logger.info("amazon-enrich-affiliates: no hay candidatos")
            return report

        if self.extractor is None:
            for outbox_id, payload in candidates:
                self._persist_failure(outbox_id, payload, error="no_extractor", status="pending")
                report.outcomes.append(
                    EnrichmentOutcome(
                        outbox_id=outbox_id,
                        canonical_url=payload.get("canonical_url"),
                        error="no_extractor",
                        status="pending",
                    )
                )
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

    def _select_candidates(self, *, limit: int) -> list[tuple[int, dict]]:
        rows = self.db.execute(
            """
            SELECT id, message_payload_json
              FROM outbox
             WHERE state IN ('pending', 'in_flight')
               AND message_payload_json LIKE '%"marketplace":%amazon%'
             ORDER BY id ASC
            """
        ).fetchall()

        candidates: list[tuple[int, dict]] = []
        for row in rows:
            try:
                payload = json.loads(row["message_payload_json"])
            except (json.JSONDecodeError, TypeError):
                continue

            if (payload.get("marketplace") or "").lower() != "amazon":
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
            error = f"unexpected: {exc}"
            self._persist_failure(outbox_id, payload, error=error, status="failed")
            return EnrichmentOutcome(
                outbox_id=outbox_id,
                canonical_url=canonical_url,
                error=error,
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
            status="ok",
        )

    def _persist_success(self, outbox_id: int, payload: dict, info: AffiliateInfo) -> None:
        new_payload = dict(payload)
        new_payload["affiliate_url"] = info.affiliate_url
        new_payload["affiliate_status"] = "ok"
        new_payload["affiliate_error"] = None
        new_payload["affiliate_enriched_at"] = _now_iso()
        new_payload["affiliate_store_id"] = info.store_id
        new_payload["affiliate_tracking_id"] = info.tracking_id
        new_payload["affiliate_commission_category"] = info.commission_category
        new_payload["affiliate_commission_rate"] = info.commission_rate
        new_payload["url"] = info.affiliate_url
        self._update_outbox_payload(outbox_id, new_payload)
        self._update_product_affiliate(payload, info)

    def _persist_failure(
        self, outbox_id: int, payload: dict, *, error: str, status: str
    ) -> None:
        new_payload = dict(payload)
        new_payload["affiliate_status"] = status
        new_payload["affiliate_error"] = error
        new_payload["affiliate_enriched_at"] = _now_iso()
        self._update_outbox_payload(outbox_id, new_payload)

    def _update_outbox_payload(self, outbox_id: int, payload: dict) -> None:
        self.db.execute(
            "UPDATE outbox SET message_payload_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), outbox_id),
        )

    def _update_product_affiliate(self, payload: dict, info: AffiliateInfo) -> None:
        canonical = payload.get("canonical_url")
        if not canonical or not info.affiliate_url:
            return
        try:
            self.db.execute(
                "UPDATE products SET affiliate_link = COALESCE(?, affiliate_link) "
                "WHERE url_canonical = ?",
                (info.affiliate_url, canonical),
            )
        except sqlite3.Error as exc:
            logger.debug("amazon update_product_affiliate failed: %s", exc)


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
