"""Helpers para el adapter Telethon.

Vive en archivo separado para que los tests no necesiten Telethon: el
módulo se importa lazy desde `telethon_listener.py`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..agents.telegram_listener_agent import IncomingMessage


logger = logging.getLogger(__name__)


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._\-]+")


async def incoming_message_from_telethon(
    msg: Any,
    *,
    channel: str,
    chat_id: int,
    client: Any,
    images_root: Path,
    download_images: bool,
) -> IncomingMessage:
    """Convierte un Message de Telethon en `IncomingMessage` neutral."""
    text = getattr(msg, "text", None) or getattr(msg, "message", None) or ""
    date = getattr(msg, "date", None) or datetime.now(timezone.utc)
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)

    image_path: Optional[str] = None
    if download_images and getattr(msg, "media", None) is not None:
        try:
            from telethon.tl.types import MessageMediaPhoto  # type: ignore

            if isinstance(msg.media, MessageMediaPhoto):
                safe_channel = _FILENAME_SAFE_RE.sub("_", channel)[:60] or "channel"
                target_dir = images_root / safe_channel
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / f"{msg.id}.jpg"
                if not target.exists():
                    await client.download_media(msg, file=str(target))
                image_path = str(target)
        except Exception as exc:
            logger.debug("download_media failed (channel=%s msg=%d): %s", channel, msg.id, exc)

    return IncomingMessage(
        chat_id=int(chat_id),
        channel=channel,
        message_id=int(msg.id),
        text=text,
        date=date,
        image_path=image_path,
    )
