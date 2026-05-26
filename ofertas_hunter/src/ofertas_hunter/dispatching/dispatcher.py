"""OutboxDispatcher serializado.

Loop principal:

```
while not stop:
    refresh_outbox()
    revalidate_old_items()
    if scheduler.is_active() is False:
        sleep_until_active()
        continue
    pick = outbox.pick_random_eligible(now, last_normal_at)
    if pick is None:
        await sleep(idle_seconds)
        continue
    publish(pick) -> persist published_messages -> mark outbox SENT/FAILED
```

Es **estrictamente serializado**: no se publica más de una oferta a la vez.

Reglas duras (spec §6 + §14):

- Cooldown global 5 minutos para `normal`.
- Bypass para `price_error` y `possible_pe` ya validados.
- Selección aleatoria entre los elegibles (con prioridad de PE).
- Revalidación obligatoria si `enqueued_at` > 1h.
- Imagen, precio y URL obligatorios al publicar.
- Cada intento queda registrado en `published_messages`.
- **Hibernación**: si el scheduler dice que no es horario activo, el
  dispatcher se queda dormido sin publicar (ni siquiera errores de precio:
  la gente está dormida y publicar a las 3 AM no aporta valor).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, Protocol

from ..models import OutboxItem, OutboxState, OutboxType
from ..runtime.scheduler import OperatingScheduler, ScheduleMode
from .outbox import InMemoryOutbox
from ..publishing.whatsapp_publisher import PublishOutcome, WhatsAppPublisher


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tipos auxiliares
# ---------------------------------------------------------------------------


@dataclass
class RevalidationResult:
    """Resultado de revalidar un OutboxItem antes de publicar.

    El revalidator devuelve un payload nuevo y un flag `still_eligible`. Si no
    sigue siendo elegible, el dispatcher lo descarta con la razón.
    """

    still_eligible: bool
    payload: Optional[dict] = None
    discard_reason: Optional[str] = None


class Revalidator(Protocol):
    """Interfaz que el dispatcher invoca cuando outbox age > 1h.

    En Fase 3.1 no hay implementación real (Playwright entra en 3.3/3.4).
    Para tests se inyecta un fake.
    """

    async def revalidate(self, item: OutboxItem) -> RevalidationResult: ...


class NullRevalidator:
    """Revalidator por defecto: deja pasar tal cual.

    Útil cuando todavía no hay marketplaces implementados (Fase 3.1).
    El dispatcher hace por sí mismo los gates de imagen/precio/url, así que
    no es peligroso.
    """

    async def revalidate(self, item: OutboxItem) -> RevalidationResult:
        return RevalidationResult(still_eligible=True, payload=item.message_payload)


# ---------------------------------------------------------------------------
# Persistencia de published_messages (opcional)
# ---------------------------------------------------------------------------


PublishedRecorder = Callable[[OutboxItem, PublishOutcome, datetime], Awaitable[None]]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class OutboxDispatcher:
    """Dispatcher serializado del outbox a WhatsApp."""

    def __init__(
        self,
        outbox: InMemoryOutbox,
        publisher: WhatsAppPublisher,
        *,
        revalidator: Optional[Revalidator] = None,
        published_recorder: Optional[PublishedRecorder] = None,
        idle_sleep_seconds: float = 5.0,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        scheduler: Optional[OperatingScheduler] = None,
    ) -> None:
        self.outbox = outbox
        self.publisher = publisher
        self.revalidator = revalidator or NullRevalidator()
        self._record_published = published_recorder
        self.idle_sleep_seconds = idle_sleep_seconds
        self._clock = clock
        self.scheduler = scheduler
        self._last_normal_publication_at: Optional[datetime] = None
        self._stop = asyncio.Event()
        self._publish_lock = asyncio.Lock()
        self._last_mode_logged: Optional[str] = None

    # ------------------------------------------------------------------
    # Loop público
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        logger.info("OutboxDispatcher arrancando (idle_sleep=%.1fs)", self.idle_sleep_seconds)
        try:
            while not self._stop.is_set():
                await self.tick()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.idle_sleep_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            logger.info("OutboxDispatcher detenido")

    async def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Un ciclo
    # ------------------------------------------------------------------

    async def tick(self) -> Optional[PublishOutcome]:
        """Ejecuta un sólo ciclo: revalidación + selección + publicación.

        Devuelve el `PublishOutcome` cuando se publica algo, `None` si no hay
        nada elegible o el scheduler está hibernando.
        """
        async with self._publish_lock:
            now = self._clock()

            # Hibernación: ni siquiera revalidamos para no quemar Playwright.
            if self._is_hibernating():
                return None

            await self._revalidate_old_items(now)

            picked = self.outbox.pick_random_eligible(
                last_normal_publication_at=self._last_normal_publication_at,
                now=now,
            )
            if picked is None:
                return None

            return await self._publish_item(picked, now)

    def _is_hibernating(self) -> bool:
        if self.scheduler is None:
            return False
        decision = self.scheduler.decide()
        # Hibernating + warmup → ambos pausan publicación.
        is_paused = decision.mode in (ScheduleMode.HIBERNATING, ScheduleMode.WARMUP)
        if is_paused:
            current = decision.mode.value
            if self._last_mode_logged != current:
                logger.info(
                    "dispatcher pausado: modo=%s (próximo=%s en %s)",
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

    # ------------------------------------------------------------------
    # Revalidación
    # ------------------------------------------------------------------

    async def _revalidate_old_items(self, now: datetime) -> None:
        old_items = self.outbox.needs_revalidation(now=now)
        for item in old_items:
            logger.info(
                "Revalidando outbox item id=%s (age %ds)",
                item.id,
                int((now - item.enqueued_at).total_seconds()),
            )
            try:
                result = await self.revalidator.revalidate(item)
            except Exception as exc:  # nunca dejamos morir el dispatcher
                logger.warning("revalidator error item=%s: %s", item.id, exc)
                self.outbox.mark_discarded(item, reason=f"revalidation_error: {exc}")
                continue

            if not result.still_eligible:
                reason = result.discard_reason or "no_longer_eligible"
                self.outbox.mark_discarded(item, reason=reason)
                logger.info("Outbox item id=%s descartado: %s", item.id, reason)
                continue

            if result.payload is not None and result.payload != item.message_payload:
                self.outbox.update_after_revalidation(item, result.payload, now=now)

    # ------------------------------------------------------------------
    # Publicación
    # ------------------------------------------------------------------

    async def _publish_item(self, item: OutboxItem, now: datetime) -> PublishOutcome:
        outcome = await self.publisher.publish(item)

        if outcome.skipped:
            # Publishing está deshabilitado: dejamos el item pendiente.
            logger.info(
                "Publish skipped (publishing_disabled) item=%s — se mantiene pending",
                item.id,
            )
            return outcome

        # REGLA 6: si el publisher pidió descartar (medium-PE sin descuento),
        # marcamos el item como discarded para que NO se reintente.
        if outcome.discard_reason is not None:
            self.outbox.mark_discarded(item, reason=outcome.discard_reason)
            logger.warning(
                "Discarded outbox item=%s (publisher discard_reason=%s)",
                item.id,
                outcome.discard_reason,
            )
            return outcome

        if outcome.success:
            # Si el item fue degradado (PE → normal), persistimos el cambio
            # antes de marcar como SENT para que el histórico refleje el
            # tipo final correcto y futuras analíticas lo computen como
            # oferta normal.
            if outcome.degraded_outbox_type is not None:
                from dataclasses import replace as _replace

                degraded = _replace(
                    item,
                    type=outcome.degraded_outbox_type,
                    message_payload=outcome.degraded_payload or item.message_payload,
                )
                self.outbox._update(  # noqa: SLF001 — uso interno legítimo
                    item,
                    type=degraded.type,
                    message_payload=degraded.message_payload,
                )
                item = degraded
            self.outbox.mark_published(item, now=now)
            if item.type == OutboxType.NORMAL.value and not outcome.dry_run:
                # Cooldown sólo aplica para envíos reales: en dry-run no
                # bloqueamos siguientes ofertas (útil para tests y staging).
                self._last_normal_publication_at = now
            elif item.type == OutboxType.NORMAL.value:
                # En dry-run actualizamos también para no spamear logs si el
                # operador lo desea. Lo mantenemos para tener cooldown realista.
                self._last_normal_publication_at = now
            logger.info(
                "Published item=%s type=%s dry_run=%s",
                item.id,
                item.type,
                outcome.dry_run,
            )
        else:
            self.outbox.mark_failed(item, now=now)
            logger.warning(
                "Publish failed item=%s error=%s",
                item.id,
                outcome.error,
            )

        if self._record_published is not None:
            try:
                await self._record_published(item, outcome, now)
            except Exception as exc:
                logger.warning("published_recorder failed: %s", exc)

        return outcome

    # ------------------------------------------------------------------
    # Hooks útiles para tests / startup
    # ------------------------------------------------------------------

    def reset_cooldown(self) -> None:
        self._last_normal_publication_at = None

    def set_last_normal_publication(self, when: Optional[datetime]) -> None:
        self._last_normal_publication_at = when


# ---------------------------------------------------------------------------
# Recorder simple a SQLite (opcional, usado por el orchestrator)
# ---------------------------------------------------------------------------


def make_sqlite_published_recorder(conn) -> PublishedRecorder:
    """Crea un recorder que persiste publishedMessages a SQLite.

    `conn` es una `sqlite3.Connection`.
    """

    async def _record(
        item: OutboxItem, outcome: PublishOutcome, when: datetime
    ) -> None:
        text = outcome.formatted.text if outcome.formatted else ""
        media_url = outcome.formatted.image_url if outcome.formatted else None
        evolution_response = (
            json.dumps(outcome.evolution_response.raw, ensure_ascii=False)
            if outcome.evolution_response and outcome.evolution_response.raw
            else None
        )
        conn.execute(
            "INSERT INTO published_messages "
            "(outbox_id, offer_id, sent_at, success, evolution_response, message_text, media_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                item.id,
                item.offer_id,
                when.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
                    "+00:00", "Z"
                ),
                1 if outcome.success else 0,
                evolution_response,
                text,
                media_url,
            ),
        )

    return _record
