"""LegacyAmazonHunterAgent: hunter Amazon que usa el worker anti-captcha
del scraper legacy `AmazonScrapperIA` y persiste resultados con el
`PriceErrorScorer` y los modelos del bot nuevo.

Diseñado para sustituir a `AmazonHunterAgent` cuando
`settings.amazon_hunter_legacy=True`. Mantiene la misma interfaz pública
mínima (`hunt_from_frontier(max_urls)` + `aclose()`) para que el
`AgentFactoryBuilder` no tenga que cambiar su cuerpo.

Diferencias visibles desde fuera:
- En cada `hunt_one` no usa `AmazonProductParser` ni el `BrowserWorker`
  del bot nuevo. Usa `legacy_amazon.LegacyAmazonWorker`.
- Las decisiones de captcha vienen del `LegacyFetchResult`: distingue
  high vs medium confidence (criterio C). Sólo high pausa.
- Encola via `legacy_amazon.adapter.process_legacy_fetch_result`, que
  reusa `PriceErrorScorer` (criterio A: scoring final = bot nuevo).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from ..intelligence.price_error_scorer import PriceErrorScorer
from ..marketplaces.base import ExtractedProduct
from .legacy_amazon.adapter import (
    AdapterOutcome,
    process_legacy_fetch_result,
)
from .legacy_amazon.worker import LegacyAmazonWorker, LegacyFetchResult


logger = logging.getLogger(__name__)


@dataclass
class LegacyHuntOutcome:
    """Forma compatible con `HuntOutcome` del hunter del bot nuevo.

    Mantiene los mismos atributos que el orquestador inspecciona
    (`enqueued_outbox_id`, `discarded_reason`, `captcha_*`) para que el
    cuerpo de `_loop` no tenga que ramificarse según el tipo.
    """

    url: str
    final_url: str
    extracted: Optional[ExtractedProduct]
    classification: Optional[str]
    suggested_outbox_type: Optional[str]
    enqueued_outbox_id: Optional[int]
    discarded_reason: Optional[str]
    snapshot_id: Optional[int] = None
    captcha_confidence: Optional[str] = None
    captcha_should_pause_marketplace: bool = False
    captcha_strong_signals: tuple[str, ...] = field(default_factory=tuple)
    captcha_visible_signals: tuple[str, ...] = field(default_factory=tuple)
    captcha_weak_signals: tuple[str, ...] = field(default_factory=tuple)
    captcha_debug_path: Optional[str] = None


class LegacyAmazonHunterAgent:
    def __init__(
        self,
        *,
        worker: Optional[LegacyAmazonWorker] = None,
        db_conn: Optional[sqlite3.Connection] = None,
        scorer: Optional[PriceErrorScorer] = None,
        normal_offer_min_discount: float = 50.0,
        warmup_homepage: bool = True,
        delay_between_requests_ms: tuple[int, int] = (8000, 15000),
    ) -> None:
        self.worker = worker or LegacyAmazonWorker(
            headless=True,
            warmup_homepage=warmup_homepage,
            delay_between_requests_ms=delay_between_requests_ms,
        )
        self.db = db_conn
        self.scorer = scorer or PriceErrorScorer()
        self.normal_offer_min_discount = normal_offer_min_discount

    async def aclose(self) -> None:
        try:
            await self.worker.aclose()
        except Exception:
            pass

    async def hunt_one(self, url: str) -> LegacyHuntOutcome:
        if self.db is None:
            return LegacyHuntOutcome(
                url=url,
                final_url=url,
                extracted=None,
                classification=None,
                suggested_outbox_type=None,
                enqueued_outbox_id=None,
                discarded_reason="no_db_conn",
            )
        logger.info("legacy_amazon_hunter fetch %s", url)
        result: LegacyFetchResult = await self.worker.fetch_product(url)
        outcome: AdapterOutcome = process_legacy_fetch_result(
            self.db,
            self.scorer,
            result,
            original_url=url,
            normal_offer_min_discount=self.normal_offer_min_discount,
        )
        return LegacyHuntOutcome(
            url=url,
            final_url=outcome.final_url,
            extracted=outcome.extracted,
            classification=outcome.classification,
            suggested_outbox_type=None,
            enqueued_outbox_id=outcome.enqueued_outbox_id,
            discarded_reason=outcome.discarded_reason,
            captcha_confidence=outcome.captcha_confidence,
            captcha_should_pause_marketplace=(
                outcome.captcha and outcome.captcha_confidence == "high"
            ),
            captcha_strong_signals=outcome.captcha_signals,
        )

    async def hunt_urls(
        self, urls: list[str], *, max_urls: Optional[int] = None
    ) -> list[LegacyHuntOutcome]:
        target = urls[:max_urls] if max_urls else urls
        outcomes: list[LegacyHuntOutcome] = []
        for url in target:
            outcomes.append(await self.hunt_one(url))
        return outcomes

    async def hunt_from_frontier(self, *, max_urls: int = 5) -> list[LegacyHuntOutcome]:
        """Procesa URLs `kind=product` del frontier compartido del bot nuevo.

        Criterio 5 de la spec: el LegacyAmazonHunterAgent NO usa frontier
        propio; consume el del bot nuevo, igual que `AmazonHunterAgent`.
        """
        if self.db is None:
            return []
        from ..exploration.frontier import FrontierRepo

        frontier = FrontierRepo(self.db)
        items = frontier.pop("amazon", kind="product", limit=max_urls)
        if not items:
            return []
        outcomes: list[LegacyHuntOutcome] = []
        for item in items:
            try:
                outcomes.append(await self.hunt_one(item.url))
            except Exception as exc:
                logger.warning(
                    "legacy_amazon_hunter.hunt_one raised on %s: %s", item.url, exc
                )
                continue
        return outcomes
