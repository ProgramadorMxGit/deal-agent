"""Serialized outbox dispatcher."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional, Protocol

from ..db import execute_with_retry, is_locked_error
from ..models import OutboxItem, OutboxType
from ..publishing.whatsapp_publisher import PublishOutcome, WhatsAppPublisher
from ..runtime.scheduler import OperatingScheduler, ScheduleMode
from .outbox import InMemoryOutbox


logger = logging.getLogger(__name__)


@dataclass
class RevalidationResult:
    still_eligible: bool
    payload: Optional[dict] = None
    discard_reason: Optional[str] = None


@dataclass
class TickAttemptResult:
    outcome: Optional[PublishOutcome]
    terminal: bool
    attempts_used: int


class Revalidator(Protocol):
    async def revalidate(self, item: OutboxItem) -> RevalidationResult: ...


class NullRevalidator:
    async def revalidate(self, item: OutboxItem) -> RevalidationResult:
        return RevalidationResult(still_eligible=True, payload=item.message_payload)


PublishedRecorder = Callable[[OutboxItem, PublishOutcome, datetime], Awaitable[None]]
DuplicateChecker = Callable[[OutboxItem], bool]
ItemSelector = Callable[
    [InMemoryOutbox, Optional[datetime], datetime],
    Awaitable[Optional[OutboxItem]],
]


class OutboxDispatcher:
    """Dispatches a single outbox item at a time."""

    def __init__(
        self,
        outbox: InMemoryOutbox,
        publisher: WhatsAppPublisher,
        *,
        revalidator: Optional[Revalidator] = None,
        published_recorder: Optional[PublishedRecorder] = None,
        duplicate_checker: Optional[DuplicateChecker] = None,
        item_selector: Optional[ItemSelector] = None,
        idle_sleep_seconds: float = 5.0,
        revalidation_budget_per_tick: int = 2,
        max_attempts_per_tick: int = 10,
        temporary_failure_retry_seconds: int = 120,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        scheduler: Optional[OperatingScheduler] = None,
    ) -> None:
        self.outbox = outbox
        self.publisher = publisher
        self.revalidator = revalidator or NullRevalidator()
        self._record_published = published_recorder
        self._is_duplicate = duplicate_checker
        self._item_selector = item_selector
        self.idle_sleep_seconds = idle_sleep_seconds
        self.revalidation_budget_per_tick = max(1, int(revalidation_budget_per_tick))
        self.max_attempts_per_tick = max(1, int(max_attempts_per_tick))
        self.temporary_failure_retry_seconds = max(
            1, int(temporary_failure_retry_seconds)
        )
        self._clock = clock
        self.scheduler = scheduler
        self._last_normal_publication_at: Optional[datetime] = None
        self._stop = asyncio.Event()
        self._publish_lock = asyncio.Lock()
        self._last_mode_logged: Optional[str] = None
        selector_name = "diversity_curator" if item_selector is not None else "legacy"
        logger.info("OutboxDispatcher configured selector=%s", selector_name)

    async def run_forever(self) -> None:
        logger.info("OutboxDispatcher arrancando (idle_sleep=%.1fs)", self.idle_sleep_seconds)
        try:
            while not self._stop.is_set():
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("dispatcher tick failed; continuing next cycle")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.idle_sleep_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            logger.info("OutboxDispatcher detenido")

    async def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> Optional[PublishOutcome]:
        async with self._publish_lock:
            now = self._clock()
            if self._is_hibernating():
                return None

            attempted_ids: set[int] = set()
            attempts_remaining = self.max_attempts_per_tick
            last_soft_outcome: Optional[PublishOutcome] = None
            needs_revalidation = self.outbox.needs_revalidation(now=now)
            hard_revalidation_ids = {
                item.id
                for item in needs_revalidation
                if (item.message_payload or {}).get("requires_live_validation") is True
            }

            fresh_ids = {
                item.id
                for item in self.outbox.eligible_now(self._last_normal_publication_at, now)
                if item.id not in hard_revalidation_ids
            }
            if fresh_ids:
                fresh_result = await self._attempt_candidates(
                    now,
                    attempted_ids=attempted_ids,
                    attempts_remaining=attempts_remaining,
                    allowed_ids=fresh_ids,
                )
                attempts_remaining -= fresh_result.attempts_used
                if fresh_result.terminal:
                    return fresh_result.outcome
                last_soft_outcome = fresh_result.outcome

            if attempts_remaining <= 0:
                return last_soft_outcome

            await self._revalidate_old_items(now)
            post_result = await self._attempt_candidates(
                now,
                attempted_ids=attempted_ids,
                attempts_remaining=attempts_remaining,
                allowed_ids=None,
            )
            if post_result.terminal:
                return post_result.outcome
            return post_result.outcome or last_soft_outcome

    def _is_hibernating(self) -> bool:
        if self.scheduler is None:
            return False
        decision = self.scheduler.decide()
        is_paused = decision.mode in (ScheduleMode.HIBERNATING, ScheduleMode.WARMUP)
        if is_paused:
            current = decision.mode.value
            if self._last_mode_logged != current:
                logger.info(
                    "dispatcher pausado: modo=%s (proximo=%s en %s)",
                    decision.mode.value,
                    decision.next_mode.value,
                    decision.next_change_in,
                )
                self._last_mode_logged = current
        else:
            if self._last_mode_logged not in (None, "active"):
                logger.info("dispatcher reactivado: modo=active")
            self._last_mode_logged = "active"
        return is_paused

    async def _revalidate_old_items(self, now: datetime) -> None:
        old_items = self.outbox.needs_revalidation(now=now)[
            : self.revalidation_budget_per_tick
        ]
        for item in old_items:
            logger.info(
                "Revalidando outbox item id=%s (age %ds)",
                item.id,
                int((now - item.enqueued_at).total_seconds()),
            )
            try:
                result = await self.revalidator.revalidate(item)
            except Exception as exc:
                logger.warning("revalidator error item=%s: %s", item.id, exc)
                self._safe_outbox_write(
                    "revalidation_error_discard",
                    item,
                    lambda: self.outbox.mark_discarded(
                        item, reason=f"revalidation_error: {exc}"
                    ),
                )
                continue

            if not result.still_eligible:
                reason = result.discard_reason or "no_longer_eligible"
                if self._safe_outbox_write(
                    "revalidation_discard",
                    item,
                    lambda: self.outbox.mark_discarded(item, reason=reason),
                ):
                    logger.info("Outbox item id=%s descartado: %s", item.id, reason)
                continue

            refreshed_payload = result.payload or item.message_payload
            self._safe_outbox_write(
                "revalidation_update",
                item,
                lambda: self.outbox.update_after_revalidation(
                    item,
                    refreshed_payload,
                    now=now,
                ),
            )

    async def _publish_item(self, item: OutboxItem, now: datetime) -> PublishOutcome:
        if self._is_duplicate is not None:
            try:
                already = bool(self._is_duplicate(item))
            except Exception:
                logger.exception("duplicate_checker raised on item=%s", item.id)
                already = False
            if already:
                logger.info(
                    "Publish skipped (recent_duplicate) item=%s - descartando",
                    item.id,
                )
                self._safe_outbox_write(
                    "duplicate_discard",
                    item,
                    lambda: self.outbox.mark_discarded(
                        item, reason="recent_duplicate_dispatched"
                    ),
                )
                return PublishOutcome(
                    success=False,
                    dry_run=False,
                    formatted=None,
                    evolution_response=None,
                    skipped=True,
                    skip_reason="recent_duplicate",
                )

        outcome = await self.publisher.publish(item)

        if outcome.skipped:
            logger.info(
                "Publish skipped (publishing_disabled) item=%s - se mantiene pending",
                item.id,
            )
            return outcome

        if outcome.discard_reason is not None:
            self._safe_outbox_write(
                "publisher_discard",
                item,
                lambda: self.outbox.mark_discarded(item, reason=outcome.discard_reason),
            )
            logger.warning(
                "Discarded outbox item=%s (publisher discard_reason=%s)",
                item.id,
                outcome.discard_reason,
            )
            return outcome

        if outcome.success:
            if outcome.degraded_outbox_type is not None:
                from dataclasses import replace as _replace

                degraded = _replace(
                    item,
                    type=outcome.degraded_outbox_type,
                    message_payload=outcome.degraded_payload or item.message_payload,
                )
                self._safe_outbox_write(
                    "degraded_update",
                    item,
                    lambda: self.outbox._update(
                        item,
                        type=degraded.type,
                        message_payload=degraded.message_payload,
                    ),
                )
                item = degraded
            self._safe_outbox_write(
                "mark_published",
                item,
                lambda: self.outbox.mark_published(item, now=now),
            )
            if item.type == OutboxType.NORMAL.value:
                self._last_normal_publication_at = now
            logger.info(
                "Published item=%s type=%s dry_run=%s",
                item.id,
                item.type,
                outcome.dry_run,
            )
        else:
            if (
                outcome.evolution_response is not None
                and outcome.evolution_response.temporary
            ):
                self._safe_outbox_write(
                    "mark_retry_later",
                    item,
                    lambda: self.outbox.mark_retry_later(
                        item,
                        now=now,
                        delay_seconds=self.temporary_failure_retry_seconds,
                        error=outcome.error,
                    ),
                )
                logger.warning(
                    "Publish temporary failure item=%s error=%s retry_in=%ss",
                    item.id,
                    outcome.error,
                    self.temporary_failure_retry_seconds,
                )
            else:
                self._safe_outbox_write(
                    "mark_failed",
                    item,
                    lambda: self.outbox.mark_failed(item, now=now),
                )
                logger.warning("Publish failed item=%s error=%s", item.id, outcome.error)

        if self._record_published is not None:
            try:
                await self._record_published(item, outcome, now)
            except Exception as exc:
                logger.warning("published_recorder failed: %s", exc)

        return outcome

    async def _attempt_candidates(
        self,
        now: datetime,
        *,
        attempted_ids: set[int],
        attempts_remaining: int,
        allowed_ids: Optional[set[int]],
    ) -> TickAttemptResult:
        attempts_used = 0
        last_soft_outcome: Optional[PublishOutcome] = None

        while attempts_used < attempts_remaining:
            picked = await self._pick_candidate(
                now,
                excluded_ids=attempted_ids,
                allowed_ids=allowed_ids,
            )
            if picked is None:
                break
            attempted_ids.add(picked.id)
            attempts_used += 1
            outcome = await self._publish_item(picked, now)
            if not self._should_continue_search(outcome):
                return TickAttemptResult(
                    outcome=outcome,
                    terminal=True,
                    attempts_used=attempts_used,
                )
            last_soft_outcome = outcome

        if attempts_used >= attempts_remaining and last_soft_outcome is not None:
            logger.warning(
                "dispatcher agotó max_attempts_per_tick=%s sin publish exitoso",
                self.max_attempts_per_tick,
            )
        return TickAttemptResult(
            outcome=last_soft_outcome,
            terminal=False,
            attempts_used=attempts_used,
        )

    def reset_cooldown(self) -> None:
        self._last_normal_publication_at = None

    def set_last_normal_publication(self, when: Optional[datetime]) -> None:
        self._last_normal_publication_at = when

    async def _pick_candidate(
        self,
        now: datetime,
        *,
        excluded_ids: Optional[set[int]] = None,
        allowed_ids: Optional[set[int]] = None,
    ) -> Optional[OutboxItem]:
        candidate_pool = _CandidatePoolView(
            self.outbox,
            excluded_ids=excluded_ids,
            allowed_ids=allowed_ids,
        )
        if self._item_selector is not None:
            try:
                return await self._item_selector(
                    candidate_pool,
                    self._last_normal_publication_at,
                    now,
                )
            except Exception:
                logger.exception(
                    "DiversityCurator failed, falling back to legacy selector (item_selector)"
                )
        return candidate_pool.pick_random_eligible(
            last_normal_publication_at=self._last_normal_publication_at,
            now=now,
        )

    def _should_continue_search(self, outcome: PublishOutcome) -> bool:
        if outcome.success:
            return False
        if outcome.skipped:
            return outcome.skip_reason == "recent_duplicate"
        if outcome.discard_reason is not None:
            return True
        if outcome.evolution_response is not None:
            return False
        if outcome.error == "target_group_id_unset":
            return False
        return True

    def _safe_outbox_write(
        self,
        action: str,
        item: OutboxItem,
        operation: Callable[[], object],
    ) -> bool:
        try:
            operation()
            return True
        except sqlite3.OperationalError as exc:
            if is_locked_error(exc):
                logger.warning("outbox write lock action=%s item=%s: %s", action, item.id, exc)
                return False
            logger.exception("outbox write failed action=%s item=%s", action, item.id)
            return False
        except Exception:
            logger.exception("outbox write failed action=%s item=%s", action, item.id)
            return False


class _CandidatePoolView:
    def __init__(
        self,
        outbox: InMemoryOutbox,
        *,
        excluded_ids: Optional[set[int]] = None,
        allowed_ids: Optional[set[int]] = None,
    ) -> None:
        self._outbox = outbox
        self._excluded_ids = excluded_ids or set()
        self._allowed_ids = allowed_ids
        self._rng = getattr(outbox, "_rng", None)

    def eligible_now(
        self,
        last_normal_publication_at: Optional[datetime],
        now: Optional[datetime] = None,
    ) -> list[OutboxItem]:
        base = self._outbox.eligible_now(last_normal_publication_at, now)
        return [
            item
            for item in base
            if item.id not in self._excluded_ids
            and (self._allowed_ids is None or item.id in self._allowed_ids)
        ]

    def pick_random_eligible(
        self,
        last_normal_publication_at: Optional[datetime],
        now: Optional[datetime] = None,
    ) -> Optional[OutboxItem]:
        eligible = self.eligible_now(last_normal_publication_at, now)
        if not eligible:
            return None
        price_errors = [
            item
            for item in eligible
            if item.type in (OutboxType.PRICE_ERROR.value, OutboxType.POSSIBLE_PE.value)
        ]
        pool = price_errors or eligible
        if self._rng is not None:
            return self._rng.choice(pool)
        return pool[0]


def make_sqlite_published_recorder(conn) -> PublishedRecorder:
    async def _record(item: OutboxItem, outcome: PublishOutcome, when: datetime) -> None:
        text = outcome.formatted.text if outcome.formatted else ""
        media_url = outcome.formatted.image_url if outcome.formatted else None
        # Trazabilidad: prefijar con el tipo de media realmente enviado
        # ("screenshot" del PDP vs "image_url" público de fallback) para poder
        # auditar en producción si la captura está funcionando.
        if getattr(outcome, "media_kind", None) and media_url is not None:
            media_url = f"[{outcome.media_kind}:{outcome.media_bytes or 0}] {media_url}"
        evolution_response = (
            json.dumps(outcome.evolution_response.raw, ensure_ascii=False)
            if outcome.evolution_response and outcome.evolution_response.raw
            else None
        )
        execute_with_retry(
            conn,
            "INSERT INTO published_messages "
            "(outbox_id, offer_id, sent_at, success, evolution_response, message_text, media_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                item.id,
                item.offer_id,
                when.astimezone(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                1 if outcome.success else 0,
                evolution_response,
                text,
                media_url,
            ),
            description=f"published_messages insert outbox_id={item.id}",
        )

    return _record


def make_sqlite_duplicate_checker(conn, *, hours: int = 48) -> DuplicateChecker:
    def _check(item: OutboxItem) -> bool:
        payload = item.message_payload or {}
        item_id = payload.get("item_id") or payload.get("asin")
        if not item_id:
            return False
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        try:
            row = execute_with_retry(
                conn,
                """
                SELECT pm.id FROM published_messages pm
                JOIN outbox o ON pm.outbox_id = o.id
                WHERE pm.success = 1
                  AND pm.sent_at >= ?
                  AND pm.outbox_id != ?
                  AND (
                    json_extract(o.message_payload_json, '$.item_id') = ?
                    OR json_extract(o.message_payload_json, '$.asin') = ?
                  )
                LIMIT 1
                """,
                (cutoff, item.id, item_id, item_id),
                description=f"duplicate_checker item={item.id}",
            ).fetchone()
        except Exception:
            logger.exception("duplicate_checker SQL fallo")
            return False
        return row is not None

    return _check


def make_stale_price_checker(*, max_age_hours: int = 4) -> DuplicateChecker:
    def _check(item: OutboxItem) -> bool:
        if item.type != "normal":
            return False
        payload = item.message_payload or {}
        if payload.get("previous_price") is None:
            return False
        age = datetime.now(timezone.utc) - item.enqueued_at
        if age.total_seconds() > max_age_hours * 3600:
            logger.info(
                "stale_price_checker: item=%s age=%.1fh > %dh - descartando",
                item.id,
                age.total_seconds() / 3600,
                max_age_hours,
            )
            return True
        return False

    return _check
