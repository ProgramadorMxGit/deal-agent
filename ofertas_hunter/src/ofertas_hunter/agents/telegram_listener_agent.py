"""Agente que orquesta el listener de Telegram.

Responsabilidades:

1. Conectar a Telegram via `TelegramAdapter` (Telethon en producción, Fake en
   tests).
2. Resolver canales `TELEGRAM_TARGET_CHANNELS` (usernames, títulos, ids).
3. Hacer backfill al arrancar (`TELEGRAM_BACKFILL_LIMIT_PER_CHANNEL`).
4. Procesar nuevos mensajes via N workers paralelos
   (`TELEGRAM_CHANNEL_WORKERS`).
5. Para cada mensaje:
   - deduplicar por `(channel, message_id)` en `telegram_messages`;
   - parsear (`message_parser.parse_message`);
   - resolver shortlink (`LinkResolver`);
   - construir candidato (`TelegramCandidateBuilder`);
   - persistir en `telegram_messages`, `discarded_candidates` u `outbox`.
6. Nunca publicar — siempre encolar como `pending_revalidation` cuando aplica.

La capa Telethon vive **detrás** de la interfaz `TelegramAdapter`. El agente
en sí no importa Telethon, así que los tests no necesitan ni la librería.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Iterable, Optional, Protocol, runtime_checkable

from ..models import OutboxItem, OutboxType, Source
from ..telegram.candidate_builder import (
    LISTENER_DEAL,
    LISTENER_IGNORED_ML,
    LISTENER_NOISE,
    LISTENER_PRICE_ERROR,
    TelegramCandidate,
    TelegramCandidateBuilder,
)
from ..telegram.channel_config import ChannelEntry
from ..telegram.link_resolver import LinkResolver
from ..telegram.message_parser import ParsedTelegramMessage, parse_message


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adapter (capa Telethon)
# ---------------------------------------------------------------------------


@dataclass
class IncomingMessage:
    """Mensaje de Telegram en formato neutral (independiente de Telethon)."""

    chat_id: int
    channel: str  # nombre legible del canal
    message_id: int
    text: str
    date: datetime
    image_path: Optional[str] = None  # si se descargó la foto


@runtime_checkable
class TelegramAdapter(Protocol):
    """Interfaz mínima que el agent espera del backend Telethon (o fake)."""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def resolve_channels(
        self, entries: Iterable[ChannelEntry]
    ) -> list[tuple[ChannelEntry, int, str]]:
        """Resuelve cada entrada a (entry, chat_id, display_name)."""
        ...

    async def fetch_history(
        self, chat_id: int, channel: str, limit: int
    ) -> AsyncIterator[IncomingMessage]:
        """Itera mensajes recientes del canal (de más nuevo a más viejo).

        Implementaciones reales deben respetar `limit` y descargar imagen si
        está habilitado.
        """
        ...

    async def listen(
        self, chat_ids: list[int], channel_names: dict[int, str]
    ) -> AsyncIterator[IncomingMessage]:
        """Itera mensajes nuevos en vivo (live).

        Termina sólo cuando el caller cancela el iterador.
        """
        ...


class MissingCredentialsError(RuntimeError):
    """Faltan api_id/api_hash o session_path."""


# ---------------------------------------------------------------------------
# Configuración del agent
# ---------------------------------------------------------------------------


@dataclass
class TelegramListenerConfig:
    enabled: bool = False
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    session_path: Optional[str] = None
    target_channels: list[ChannelEntry] = field(default_factory=list)
    backfill_limit_per_channel: int = 400
    backfill_process_budget_per_channel: int = 40
    workers: int = 3
    ignore_mercadolibre_links: bool = True
    link_resolver_timeout_seconds: float = 6.0
    link_resolver_max_redirects: int = 5
    normal_offer_min_discount: float = 50.0


# ---------------------------------------------------------------------------
# Resultado de un procesamiento
# ---------------------------------------------------------------------------


@dataclass
class ProcessingOutcome:
    message: IncomingMessage
    parsed: ParsedTelegramMessage
    candidate: TelegramCandidate
    persisted: bool
    duplicate: bool = False
    outbox_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class TelegramListenerAgent:
    """Agent que escucha canales y crea candidatos."""

    def __init__(
        self,
        config: TelegramListenerConfig,
        adapter: TelegramAdapter,
        *,
        db_conn: Optional[sqlite3.Connection] = None,
        builder: Optional[TelegramCandidateBuilder] = None,
        resolver: Optional[LinkResolver] = None,
    ) -> None:
        self.config = config
        self.adapter = adapter
        self.db = db_conn
        self.builder = builder or TelegramCandidateBuilder(
            ignore_mercadolibre_links=config.ignore_mercadolibre_links,
            normal_offer_min_discount=config.normal_offer_min_discount,
        )
        self._owns_resolver = resolver is None
        self.resolver = resolver or LinkResolver(
            timeout_seconds=config.link_resolver_timeout_seconds,
            max_redirects=config.link_resolver_max_redirects,
            cache_conn=db_conn,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "TelegramListenerAgent":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_resolver:
            await self.resolver.aclose()

    def ensure_credentials(self) -> None:
        if not self.config.enabled:
            return
        missing = [
            name
            for name, value in (
                ("api_id", self.config.api_id),
                ("api_hash", self.config.api_hash),
                ("session_path", self.config.session_path),
            )
            if value in (None, "")
        ]
        if missing:
            raise MissingCredentialsError(
                f"Telegram listener requiere credenciales completas. Faltan: {', '.join(missing)}"
            )

    # ------------------------------------------------------------------
    # Entrada principal: backfill + live
    # ------------------------------------------------------------------

    async def backfill_once(self) -> list[ProcessingOutcome]:
        """Hace una pasada de backfill por canal y devuelve outcomes.

        Útil para `--once` y para tests. No lanza si una conexión falla.
        """
        if not self.config.enabled:
            logger.info("backfill skipped: TELEGRAM_ENABLED=false")
            return []
        self.ensure_credentials()

        await self.adapter.connect()
        try:
            channels = await self.adapter.resolve_channels(self.config.target_channels)
        except Exception as exc:
            await self.adapter.disconnect()
            raise RuntimeError(f"resolve_channels failed: {exc}") from exc

        outcomes: list[ProcessingOutcome] = []
        try:
            budget = self.config.backfill_process_budget_per_channel
            for entry, chat_id, display in channels:
                processed = 0
                async for msg in self.adapter.fetch_history(
                    chat_id, display, limit=self.config.backfill_limit_per_channel
                ):
                    if processed >= budget:
                        break
                    outcome = await self._process_one(msg)
                    outcomes.append(outcome)
                    if not outcome.duplicate:
                        processed += 1
        finally:
            await self.adapter.disconnect()
        return outcomes

    async def listen_once(self) -> Optional[ProcessingOutcome]:
        """Procesa un solo mensaje en vivo (útil para `--once`)."""
        if not self.config.enabled:
            return None
        self.ensure_credentials()

        await self.adapter.connect()
        try:
            channels = await self.adapter.resolve_channels(self.config.target_channels)
            chat_ids = [chat_id for _, chat_id, _ in channels]
            names = {chat_id: name for _, chat_id, name in channels}
            async for msg in self.adapter.listen(chat_ids, names):
                outcome = await self._process_one(msg)
                return outcome
        finally:
            await self.adapter.disconnect()
        return None

    # ------------------------------------------------------------------
    # Procesamiento de un mensaje
    # ------------------------------------------------------------------

    async def _process_one(self, msg: IncomingMessage) -> ProcessingOutcome:
        if self._is_duplicate(msg):
            logger.debug(
                "telegram dedupe hit channel=%s message_id=%d", msg.channel, msg.message_id
            )
            return ProcessingOutcome(
                message=msg,
                parsed=parse_message(
                    msg.text,
                    channel=msg.channel,
                    message_id=msg.message_id,
                    captured_at=msg.date,
                    image_path=msg.image_path,
                    chat_id=msg.chat_id,
                ),
                candidate=TelegramCandidate(
                    parsed=parse_message(
                        msg.text,
                        channel=msg.channel,
                        message_id=msg.message_id,
                        captured_at=msg.date,
                        image_path=msg.image_path,
                        chat_id=msg.chat_id,
                    ),
                    internal_classification=LISTENER_NOISE,
                    reasons=["duplicate_message"],
                ),
                persisted=False,
                duplicate=True,
            )

        parsed = parse_message(
            msg.text,
            channel=msg.channel,
            message_id=msg.message_id,
            captured_at=msg.date,
            image_path=msg.image_path,
            chat_id=msg.chat_id,
        )

        # Resolver shortlinks (best-effort).
        resolved = None
        if parsed.original_url:
            try:
                resolved = await self.resolver.resolve(parsed.original_url)
                if resolved.final_url and resolved.final_url != parsed.original_url:
                    parsed.resolved_url = resolved.final_url
            except Exception as exc:
                logger.debug("link_resolver error url=%s: %s", parsed.original_url, exc)

        candidate = self.builder.build(parsed, resolved)

        outbox_id = self._persist(msg, parsed, candidate, resolved)
        return ProcessingOutcome(
            message=msg,
            parsed=parsed,
            candidate=candidate,
            persisted=outbox_id is not None or self.db is not None,
            outbox_id=outbox_id,
        )

    # ------------------------------------------------------------------
    # Persistencia SQLite
    # ------------------------------------------------------------------

    def _is_duplicate(self, msg: IncomingMessage) -> bool:
        if self.db is None:
            return False
        try:
            row = self.db.execute(
                "SELECT 1 FROM telegram_messages WHERE channel=? AND message_id=?",
                (msg.channel, msg.message_id),
            ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None

    def _persist(
        self,
        msg: IncomingMessage,
        parsed: ParsedTelegramMessage,
        candidate: TelegramCandidate,
        resolved,
    ) -> Optional[int]:
        if self.db is None:
            return None

        now = _utcnow_iso()

        # 1) telegram_messages
        try:
            self.db.execute(
                "INSERT INTO telegram_messages "
                "(channel, message_id, text, image_path, original_url, resolved_url, "
                " captured_at, processed_at, skip_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    msg.channel,
                    msg.message_id,
                    parsed.text,
                    parsed.image_path,
                    parsed.original_url,
                    parsed.resolved_url,
                    msg.date.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    now,
                    parsed.skip_reason,
                ),
            )
        except sqlite3.IntegrityError:
            logger.debug("telegram_messages duplicate insert skipped: %s/%d", msg.channel, msg.message_id)

        # 2) discarded_candidates si aplica
        if candidate.is_ignored:
            self.db.execute(
                "INSERT INTO discarded_candidates (source, raw_payload_json, reason, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    Source.TELEGRAM.value,
                    json.dumps(
                        {
                            "channel": msg.channel,
                            "message_id": msg.message_id,
                            "text": parsed.text,
                            "url": parsed.original_url,
                            "resolved_url": parsed.resolved_url,
                        },
                        ensure_ascii=False,
                    ),
                    candidate.internal_classification,
                    now,
                ),
            )
            return None

        if candidate.outbox_item is None:
            return None

        # 3) products + offers + outbox para items actionable
        outbox_id = self._enqueue_pending_revalidation(parsed, candidate, resolved)
        return outbox_id

    def _enqueue_pending_revalidation(
        self,
        parsed: ParsedTelegramMessage,
        candidate: TelegramCandidate,
        resolved,
    ) -> Optional[int]:
        assert self.db is not None
        item = candidate.outbox_item
        if item is None:
            return None

        url = item.message_payload.get("url") or parsed.original_url
        if not url:
            return None

        marketplace = parsed.marketplace or "other"

        # Reusar product si url_canonical existe
        existing = self.db.execute(
            "SELECT id FROM products WHERE url_canonical = ?", (url,)
        ).fetchone()
        if existing is not None:
            product_id = existing["id"]
        else:
            cur = self.db.execute(
                "INSERT INTO products (marketplace, url_canonical, title, condition, "
                "first_seen_at, last_seen_at) VALUES (?, ?, ?, 'unknown', ?, ?)",
                (
                    marketplace,
                    url,
                    parsed.title_guess or "(sin título)",
                    _utcnow_iso(),
                    _utcnow_iso(),
                ),
            )
            product_id = cur.lastrowid

        # Offer en estado eligible (espera revalidación Playwright)
        cur = self.db.execute(
            "INSERT INTO offers (product_id, classification, score, reasons_json, "
            "discount_percent, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                product_id,
                candidate.scoring.classification if candidate.scoring else "telegram_signal",
                candidate.scoring.score if candidate.scoring else 0,
                json.dumps(candidate.reasons, ensure_ascii=False),
                parsed.discount_visible,
                "eligible",
                _utcnow_iso(),
                _utcnow_iso(),
            ),
        )
        offer_id = cur.lastrowid

        cur = self.db.execute(
            "INSERT INTO outbox(offer_id, type, enqueued_at, scheduled_for, attempts, "
            "last_attempt_at, state, message_payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                offer_id,
                item.type,
                _utcnow_iso(),
                None,
                0,
                None,
                "pending",
                json.dumps(item.message_payload, ensure_ascii=False),
            ),
        )
        outbox_id = cur.lastrowid
        logger.info(
            "telegram candidate enqueued outbox_id=%s offer_id=%s type=%s score=%s",
            outbox_id,
            offer_id,
            item.type,
            candidate.scoring.score if candidate.scoring else "?",
        )
        return outbox_id


# ---------------------------------------------------------------------------
# Helper datetime
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
