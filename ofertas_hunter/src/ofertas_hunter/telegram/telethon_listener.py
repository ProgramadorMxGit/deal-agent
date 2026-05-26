"""Adapter Telethon: implementación real del `TelegramAdapter`.

Importa Telethon sólo cuando se instancia. Si Telethon no está instalado,
falla con un mensaje claro al construir.

Resolución de canales:
1. Username (`ofertonesmexico`) → `client.get_entity('ofertonesmexico')`.
2. Título exacto (`OFERTAS PREMIUM MX`) → buscar en `client.iter_dialogs()`.
3. Id numérico → `client.get_entity(int)`.

Backfill:
- Usa `client.iter_messages(chat, limit=N)`.
- Sólo procesa mensajes con `text` no vacío o con `media`.
- Si tiene foto, descarga a `data/telegram_images/<channel>/<message_id>.jpg`.

Live:
- `events.NewMessage(chats=[...])` se traduce a un `asyncio.Queue` y se
  expone como async iterator.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Iterable, Optional

from .channel_config import ChannelEntry


logger = logging.getLogger(__name__)


class TelethonImportError(RuntimeError):
    pass


def _require_telethon():
    try:
        from telethon import TelegramClient, events  # type: ignore
        from telethon.tl.types import (  # type: ignore  # noqa: F401
            MessageMediaPhoto,
            Channel,
            Chat,
            User,
        )
    except Exception as exc:  # pragma: no cover - solo en runtime real
        raise TelethonImportError(
            "Telethon no está instalado. Instalá con `pip install telethon` "
            "o desactivá TELEGRAM_ENABLED en .env."
        ) from exc
    return TelegramClient, events


class TelethonAdapter:
    """Adapter real con Telethon. Implementa la interfaz `TelegramAdapter`."""

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        session_path: str,
        download_images: bool = True,
        images_root: Optional[Path] = None,
    ) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path
        self.download_images = download_images
        self.images_root = images_root or Path("data/telegram_images")
        self._client = None

        TelegramClient, _events = _require_telethon()
        # No conectamos todavía: sólo en `connect()`.
        self._client_factory = lambda: TelegramClient(
            session_path, api_id, api_hash
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        if self._client is None:
            self._client = self._client_factory()
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise RuntimeError(
                "Telegram session no autorizada. Corre `scripts/test_telegram.py "
                "--login` para inicializar."
            )

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

    # ------------------------------------------------------------------
    # Resolve channels
    # ------------------------------------------------------------------

    async def resolve_channels(
        self, entries: Iterable[ChannelEntry]
    ) -> list[tuple[ChannelEntry, int, str]]:
        if self._client is None:
            raise RuntimeError("Telethon adapter not connected")

        resolved: list[tuple[ChannelEntry, int, str]] = []
        # Cache de diálogos para resolución por título.
        dialogs_cache = None

        for entry in entries:
            normalized = entry.normalized()
            chat_id: Optional[int] = None
            display = normalized
            try:
                if entry.kind == "username":
                    ent = await self._client.get_entity(normalized)
                    chat_id = int(ent.id)
                    display = getattr(ent, "username", None) or getattr(ent, "title", normalized)
                elif entry.kind == "id":
                    ent = await self._client.get_entity(int(normalized))
                    chat_id = int(ent.id)
                    display = getattr(ent, "title", None) or str(chat_id)
                else:  # title
                    if dialogs_cache is None:
                        dialogs_cache = []
                        async for dialog in self._client.iter_dialogs():
                            dialogs_cache.append(dialog)
                    for dialog in dialogs_cache:
                        title = getattr(dialog, "title", None) or getattr(dialog.entity, "title", None)
                        if title and title.strip().lower() == normalized.strip().lower():
                            chat_id = int(dialog.id)
                            display = title
                            break
            except Exception as exc:
                logger.warning("resolve_channel %r failed: %s", entry.raw, exc)

            if chat_id is None:
                logger.warning("Canal no resuelto: %r (kind=%s)", entry.raw, entry.kind)
                continue
            resolved.append((entry, chat_id, display))
        return resolved

    # ------------------------------------------------------------------
    # Backfill
    # ------------------------------------------------------------------

    async def fetch_history(
        self, chat_id: int, channel: str, limit: int
    ):  # AsyncIterator[IncomingMessage]
        from .telethon_listener_helpers import incoming_message_from_telethon

        assert self._client is not None
        async for msg in self._client.iter_messages(chat_id, limit=limit):
            if msg is None:
                continue
            yield await incoming_message_from_telethon(
                msg,
                channel=channel,
                chat_id=chat_id,
                client=self._client,
                images_root=self.images_root,
                download_images=self.download_images,
            )

    # ------------------------------------------------------------------
    # Live
    # ------------------------------------------------------------------

    async def listen(
        self, chat_ids: list[int], channel_names: dict[int, str]
    ):  # AsyncIterator[IncomingMessage]
        import asyncio

        from telethon import events  # type: ignore

        from .telethon_listener_helpers import incoming_message_from_telethon

        assert self._client is not None

        queue: asyncio.Queue = asyncio.Queue()

        @self._client.on(events.NewMessage(chats=chat_ids))  # type: ignore[misc]
        async def _handler(event):  # noqa: ANN001
            try:
                channel = channel_names.get(int(event.chat_id), str(event.chat_id))
                msg = await incoming_message_from_telethon(
                    event.message,
                    channel=channel,
                    chat_id=int(event.chat_id),
                    client=self._client,
                    images_root=self.images_root,
                    download_images=self.download_images,
                )
                await queue.put(msg)
            except Exception as exc:
                logger.warning("on_new_message handler failed: %s", exc)

        try:
            while True:
                yield await queue.get()
        finally:
            self._client.remove_event_handler(_handler)
